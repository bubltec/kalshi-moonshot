"""The ladder priced from the feed we use now against the same ladder priced from the Kalshi perp. (2026-11-13, user, after the perp-ladder study: "that's not the question, because the current ladder uses something else right? so the comparison was against whatever else we're using".)
Question, one: on the same live bets, which feed gives the ladder the better chance that the side pays, the current one or Kalshi's perp reference (KP_*), and which would have been the better gate on the buys?
  current feed   what the ladder estimator reads today for the lane's series (commodities.symbol_for): Hyperliquid for the commodity favorites (feed_ticks "hl:xyz:..."). Crypto is left out: its live lanes take the ask and read no feed for the buy (user, 2026-11-13: "for crypto we don't use anything though"); the perp against the ask is scripts/ladder_perp_buy_study.py. A series with no such feed (Brent 15-minute has no instrument: the live pricer takes the ask there) is left out and counted.
  Kalshi perp    index_ticks KP_* (watcher.perp_index).
Both go through exactly the same pricing, only the feed changes: commodities.window_basis on the last 3 hours of 15-minute boundaries (strikes from the windows table; at least 6), realized_sigma on the feed's 5-minute closes of the last 6 hours (at least 20 bars) times COMMODITY_VOL_MULT plus the offset's spread in quadrature, P(yes) = Phi(ln(center / strike) / width), tail floor, side chance, edge = chance - ask - the entry fee.
Bets: the live trade-mode 15-minute bets of the commodity favorite lanes (energy, wti, natgas, precious, industrial) since Monday 11-09 00:00 ET; only bets where BOTH feeds could price are compared (the perp ticks begin 11-11 07:45 ET, so the set starts about 11-11 09:30).
Scores, both feeds on the same bets: Brier and log loss of the chance (lower is better), the win share minus the mean chance, the Brier of the ask for reference, the share of bets each feed rated closer to the outcome. Gate test (the thresholds are fixed before the run: edge at least 0.015, the lane's min_edge, and at least 0.0): per feed, the bets kept and skipped, as booked and held to settlement (as booked: cost = ask + fee, a sale at the booked exit less the 0.02 slip and the taker fee, a resting-ask fill at its own price; fee on every leg). A bet both feeds agree to keep or skip is a tie; the table shows where they disagree. Results by lane.
    scripts/cloud_job.sh scripts/ladder_feed_compare.py
"""

import bisect
import calendar
import collections
import datetime as dt
import math
import os

from kalshi_moonshot import commodities, config, db, variants
from kalshi_moonshot.kalshifeed import _closes
from kalshi_moonshot.runner import EXIT_SLIP
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import edge as edge_of
from kalshi_moonshot.strategy import fee_per_contract as fee
from kalshi_moonshot.watcher import perp_index

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
ET = dt.timezone(dt.timedelta(hours=-5))
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731
MON = calendar.timegm((2026, 11, 9, 5, 0, 0))
SCOPE_LANES = ("commodity_energy", "commodity_wti", "commodity_natgas", "commodity_precious", "commodity_industrial")
LANE_SQL = ",".join("'%s'" % x for x in SCOPE_LANES)
GATES = (0.015, 0.0)


def dollars(r, held=False):
    ask, stake = float(r["ask"]), float(r["stake_usd"])
    cost = ask + fee(ask)
    if r["exit_price"] is not None and not held:
        px = float(r["exit_price"])
        back = px if str(r["exit_reason"] or "").startswith("curve exit (resting ask)") else px - EXIT_SLIP - fee(px)
    else:
        back = 1.0 if r["result"] == r["side"] else 0.0
    return stake * (back / cost - 1.0)


bets = st.db.execute(
    "SELECT d.ts,d.ticker,d.lane,d.side,d.ask,d.stake_usd,d.exit_price,d.exit_reason,w.series,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
    f"WHERE d.action='bet' AND d.mode='trade' AND d.stake_usd>0 AND d.ask>0 AND d.ts>? AND d.ticker LIKE '%15M-%' AND d.lane IN ({LANE_SQL}) AND w.result IN ('yes','no') ORDER BY d.ts",
    (MON,),
).fetchall()
print(f"in-scope live bets since Monday 11-09 00:00 ET, settled: {len(bets)}")

cache = {}
strikes = {}


def source_rows(src):
    if src not in cache:
        kind, ident = src
        if kind == "index":
            r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ident, MON - 7 * 3600)).fetchall()
        else:
            r = tape.execute("SELECT ts,px FROM feed_ticks WHERE symbol=? AND ts>? AND px>0 ORDER BY ts", (ident, MON - 7 * 3600)).fetchall()
        cache[src] = ([float(x[0]) for x in r], [float(x[1]) for x in r])
    return cache[src]


def current_source(series):
    if variants.crypto_index(series):
        return None  # crypto: the live lanes take the ask (always lanes), nothing is read; the perp against the ask is in ladder_perp_buy_study.py
    sym = commodities.symbol_for(series)
    return ("feed", sym) if sym and sym.startswith("hl:") else None


def refs_for(series):
    if series not in strikes:
        r = st.db.execute("SELECT close_ts,strike FROM windows WHERE series=? AND close_ts>? AND strike>0", (series, MON - 4 * 3600)).fetchall()
        strikes[series] = {int(float(x["close_ts"])) - 900: float(x["strike"]) for x in r}
    return strikes[series]


def chance(b, src):
    """The side's chance from the ladder priced off `src`, or a string saying why not."""
    ts, vs = source_rows(src)
    t = float(b["ts"])
    i = bisect.bisect_right(ts, t) - 1
    if i < 0:
        return "no tick before the bet"
    age = t - ts[i]
    if age > commodities.REALTIME_STALE_SECONDS:
        return "last tick over 120 s old"
    if not b["strike"] or float(b["strike"]) <= 0:
        return "no strike"
    a6 = bisect.bisect_right(ts, t - 6 * 3600.0)
    closes, times = _closes(list(zip(ts[a6 : i + 1], vs[a6 : i + 1], strict=True)), 300)
    feed = commodities.Series(last=vs[i], last_ts=ts[i], closes=closes, times=times)
    a4 = bisect.bisect_right(ts, t - 4 * 3600.0)
    mc, mt = _closes(list(zip(ts[a4 : i + 1], vs[a4 : i + 1], strict=True)), 60)
    minutes = {int(x): c for x, c in zip(mt, mc, strict=True)}
    refs = {T: k for T, k in refs_for(b["series"]).items() if t - commodities.WINDOW_MAX_AGE <= T <= t}
    wb = commodities.window_basis(refs, minutes, t)
    if wb is None:
        return "under 6 boundaries"
    hours = max(float(b["close_ts"]) - t, 0.0) / 3600.0
    try:
        sigma = commodities.realized_sigma(feed, hours + age / 3600.0) * s.commodity_vol_mult
    except commodities.CommodityError:
        return "under 20 bars"
    unc = max(wb.sd, s.commodity_window_unc_floor)
    p_yes = commodities.prob_above(vs[i] * math.exp(wb.basis), float(b["strike"]), math.sqrt(sigma * sigma + unc * unc))
    p_yes = min(max(p_yes, s.commodity_tail_floor), 1.0 - s.commodity_tail_floor)
    return p_yes if b["side"] == "yes" else 1.0 - p_yes


group = lambda lane: "crypto" if lane.startswith("crypto_") else "commodity favorites"  # noqa: E731
rows, why = [], collections.Counter()
for b in bets:
    cur = current_source(b["series"])
    kp = perp_index(b["series"])
    if cur is None:
        why["no feed the live ladder prices from (crypto, Brent: the ask is taken)"] += 1
        continue
    if not kp:
        why["no Kalshi perp for the market"] += 1
        continue
    c1 = chance(b, cur)
    c2 = chance(b, ("index", kp))
    if isinstance(c1, str) or isinstance(c2, str):
        why[f"current: {c1}" if isinstance(c1, str) else f"perp: {c2}"] += 1
        continue
    rows.append((b, c1, c2, dollars(b), dollars(b, True)))
print("bets left out of the comparison:")
for k, v in why.most_common():
    print(f"   {v:4d}  {k}")
if not rows:
    print("no bet priced by both feeds")
    raise SystemExit(0)
print(f"\nCOMPARED: {len(rows)} bets priced by both feeds, {hm(float(rows[0][0]['ts']))} to {hm(float(rows[-1][0]['ts']))} ET; as they happened {sum(r[3] for r in rows):+.2f} booked, {sum(r[4] for r in rows):+.2f} held")


def brier(ps, ys):
    return sum((p - y) ** 2 for p, y in zip(ps, ys, strict=True)) / len(ys)


def logloss(ps, ys):
    return -sum(math.log(min(max(p if y else 1 - p, 1e-4), 1)) for p, y in zip(ps, ys, strict=True)) / len(ys)


def scores(label, rs):
    y = [1.0 if r[0]["result"] == r[0]["side"] else 0.0 for r in rs]
    ask = [float(r[0]["ask"]) for r in rs]
    cur = [r[1] for r in rs]
    kp = [r[2] for r in rs]
    avg = [(a + b) / 2 for a, b in zip(cur, kp, strict=True)]
    closer = sum(1 for a, b, o in zip(cur, kp, y, strict=True) if abs(a - o) < abs(b - o))
    closer_kp = sum(1 for a, b, o in zip(cur, kp, y, strict=True) if abs(b - o) < abs(a - o))
    print(f"\n{label}: n={len(rs)}, won {sum(y) / len(y):.1%}")
    print(f"   Brier    ask {brier(ask, y):.4f} | current feed {brier(cur, y):.4f} | Kalshi perp {brier(kp, y):.4f} | average of the two {brier(avg, y):.4f}")
    print(f"   logloss  ask {logloss(ask, y):.4f} | current feed {logloss(cur, y):.4f} | Kalshi perp {logloss(kp, y):.4f} | average of the two {logloss(avg, y):.4f}")
    print(f"   won share minus mean chance: ask {sum(y) / len(y) - sum(ask) / len(ask):+.3f} | current {sum(y) / len(y) - sum(cur) / len(cur):+.3f} | perp {sum(y) / len(y) - sum(kp) / len(kp):+.3f}")
    print(f"   closer to the outcome: current feed {closer}, Kalshi perp {closer_kp}, tied {len(rs) - closer - closer_kp}")
    for g in GATES:
        for name, idx in (("current feed", 1), ("Kalshi perp", 2)):
            kept = [r for r in rs if edge_of(r[idx], float(r[0]["ask"])) >= g]
            skip = [r for r in rs if edge_of(r[idx], float(r[0]["ask"])) < g]
            print(f"   gate edge >= {g:.3f} on the {name:12s}: kept n={len(kept):3d} booked {sum(r[3] for r in kept):+7.2f} held {sum(r[4] for r in kept):+7.2f} | skipped n={len(skip):3d} booked {sum(r[3] for r in skip):+7.2f} held {sum(r[4] for r in skip):+7.2f} | change vs what happened {-sum(r[3] for r in skip):+7.2f}")
        agree = [r for r in rs if (edge_of(r[1], float(r[0]["ask"])) >= g) == (edge_of(r[2], float(r[0]["ask"])) >= g)]
        a = [r for r in rs if edge_of(r[1], float(r[0]["ask"])) >= g and edge_of(r[2], float(r[0]["ask"])) < g]
        b = [r for r in rs if edge_of(r[1], float(r[0]["ask"])) < g and edge_of(r[2], float(r[0]["ask"])) >= g]
        print(f"   gate {g:.3f}: the feeds agree on {len(agree)} bets; current keeps and perp skips {len(a)} (booked {sum(r[3] for r in a):+.2f}); perp keeps and current skips {len(b)} (booked {sum(r[3] for r in b):+.2f})")


scores("ALL", rows)
for gname in ("commodity favorites", "crypto"):
    g = [r for r in rows if group(r[0]["lane"]) == gname]
    if len(g) >= 10:
        scores(gname.upper(), g)
for lane in SCOPE_LANES:
    g = [r for r in rows if r[0]["lane"] == lane]
    if g:
        print(f"\n{lane}: n={len(g)}, mean ask {sum(float(r[0]['ask']) for r in g) / len(g):.3f}, mean chance current {sum(r[1] for r in g) / len(g):.3f} perp {sum(r[2] for r in g) / len(g):.3f}, won {sum(1 for r in g if r[0]['result'] == r[0]['side']) / len(g):.1%}")
print("DONE")
