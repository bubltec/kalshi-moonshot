"""Would the ladder estimator, priced from Kalshi's own perp reference, have picked the right bets? (2026-11-13, user: "why are you using the curve for buy? i thought we gave up on that. use the ladder like commodity is"; then, asked what to test: "Ladder buy from the Kalshi perp"; and on why the curve fails for buying, the property to look at: "curve equation or logic for placing a bet".)

Question, one: on the live (trade-mode) 15-minute bets the bot placed since Monday 2026-11-09 00:00 ET, what would buying only where the ladder chance from the Kalshi perp clears the lane's own min_edge have done against what those bets actually got (the lane's own exit or the settlement, as booked)?

The ladder chance, as commodities.py prices a 15-minute market (price_event, the "window" anchor), with the Kalshi perp reference (index_ticks KP_*, watcher.perp_index) as the feed, no external spot:
  center  the perp's last tick at the bet (refused when over 120 s old) times the offset learned from the last 3 hours of 15-minute boundaries: the median of ln(window strike / the perp's 1-minute close at that boundary), at least 6 boundaries (commodities.window_basis; the strikes are the windows table's)
  width   the perp's realized volatility from its 5-minute closes of the last 6 hours over the time left plus the tick's age (commodities.realized_sigma, at least 20 bars), times COMMODITY_VOL_MULT, with the offset's own spread (floored at COMMODITY_WINDOW_UNC_FLOOR) added in quadrature
  chance  P(yes) = Phi(ln(center / strike) / width), kept COMMODITY_TAIL_FLOOR from 0 and 1; the side's chance; edge = chance - ask - the entry fee (strategy.edge)
The rule: keep the bet when the edge is at least the lane's min_edge (0.015 on every live lane; lanes.wants_bet with always off); otherwise do not buy ($0). A bet the ladder cannot price (no perp for the market, the perp ticks on the tape begin 11-11, too few bars or boundaries) is left out of the comparison and counted.
Dollars: what the bet booked. Cost = ask + fee; a sale at the booked exit price less the 0.02 slip and the taker fee (a resting-ask fill at its own price, no fee); unsold = the settlement. A kept bet is a tie; a skipped loser is a bet the rule beat; a skipped winner is one it lost to.
By curve shape (rule 4): the curve model's own level, slope and curvature readings at 5 and 13 minutes (curvemodel.features_curvature) of the perp against the strike, on the side bought, in terciles of these bets.
Qualifies for a shadow lane (fixed before the run): at least 30 kept bets, the change against what happened positive, the kept bets net positive, and the kept bets positive in every shape tercile with at least 5 kept.
Also printed, to say whether the chance carries information beyond the price: the bets by edge bucket (win share against the ask and the ladder chance) and the Brier score of the ask, the ladder chance and their average.

Part B, the curve's buy equation on its own bets (no new rule): each model_buy bet's note carries the ridge's predicted net per $1 ("curve model +0.12 per $1"). Printed: the bets in terciles of that prediction against what they returned held to settlement and as booked, and the rank correlation, to see whether the equation orders its own bets at all.
    scripts/cloud_job.sh scripts/ladder_perp_buy_study.py
"""

import bisect
import calendar
import collections
import datetime as dt
import math
import os
import re

from kalshi_moonshot import commodities, config, db
from kalshi_moonshot.curvemodel import features_curvature, live_grid
from kalshi_moonshot.kalshifeed import _closes
from kalshi_moonshot.lanes import lane_specs
from kalshi_moonshot.runner import EXIT_SLIP
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import edge as edge_of
from kalshi_moonshot.strategy import fee_per_contract as fee
from kalshi_moonshot.watcher import perp_index

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
specs = lane_specs(s)
ET = dt.timezone(dt.timedelta(hours=-5))
# Scope (user, 2026-11-13: "it should only be crypto multiscale, and commodity favorite lanes"; the underdog was live only 11-09 13:47 to 11-11 05:37 ET and is out, commodity_multiscale is out too).
SCOPE_LANES = ("crypto_multiscale", "commodity_energy", "commodity_wti", "commodity_natgas", "commodity_precious", "commodity_industrial")
LANE_SQL = ",".join("'%s'" % x for x in SCOPE_LANES)

hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M")  # noqa: E731
MON = calendar.timegm((2026, 11, 9, 5, 0, 0))  # 11-09 00:00 ET
SHAPE_NAMES = ("level_5m", "slope_5m", "curv_5m", "level_13m", "slope_13m", "curv_13m")
PRED = re.compile(r"curve model ([+-]?\d+\.\d+) per \$1")


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
    "SELECT d.ts,d.ticker,d.lane,d.side,d.ask,d.stake_usd,d.exit_price,d.exit_reason,d.reason,w.series,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
    "WHERE d.action='bet' AND d.mode='trade' AND d.stake_usd>0 AND d.ask>0 AND d.ts>? AND d.ticker LIKE '%15M-%' AND d.lane IN (" + LANE_SQL + ") AND w.result IN ('yes','no') ORDER BY d.ts",
    (MON,),
).fetchall()
print(f"live trade-mode 15-minute bets since Monday 11-09 00:00 ET, settled: {len(bets)} (first {hm(float(bets[0]['ts']))}, last {hm(float(bets[-1]['ts']))} ET)")

ticks = {}  # perp index -> (ts, values)
strikes = {}  # series -> {window open ts: strike}


def perp(ix):
    if ix not in ticks:
        r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, MON - 7 * 3600)).fetchall()
        ticks[ix] = ([float(x[0]) for x in r], [float(x[1]) for x in r])
    return ticks[ix]


def refs_for(series):
    if series not in strikes:
        r = st.db.execute("SELECT close_ts,strike FROM windows WHERE series=? AND close_ts>? AND strike>0", (series, MON - 4 * 3600)).fetchall()
        strikes[series] = {int(float(x["close_ts"])) - 900: float(x["strike"]) for x in r}
    return strikes[series]


def price(b):
    """(the side's ladder chance, edge, shape readings or None) or a string saying why the bet cannot be priced."""
    kp = perp_index(b["series"])
    if not kp:
        return "the market has no Kalshi perp reference"
    ts, vs = perp(kp)
    t = float(b["ts"])
    i = bisect.bisect_right(ts, t) - 1
    if i < 0:
        return "bought before the perp ticks begin on the tape"
    age = t - ts[i]
    if age > commodities.REALTIME_STALE_SECONDS:
        return "the perp's last tick was over 120 s old"
    if not b["strike"] or float(b["strike"]) <= 0:
        return "no strike recorded for the window"
    a6 = bisect.bisect_right(ts, t - 6 * 3600.0)
    closes, times = _closes(list(zip(ts[a6 : i + 1], vs[a6 : i + 1], strict=True)), 300)
    feed = commodities.Series(last=vs[i], last_ts=ts[i], closes=closes, times=times)
    a4 = bisect.bisect_right(ts, t - 4 * 3600.0)
    mc, mt = _closes(list(zip(ts[a4 : i + 1], vs[a4 : i + 1], strict=True)), 60)
    minutes = {int(x): c for x, c in zip(mt, mc, strict=True)}
    refs = {T: k for T, k in refs_for(b["series"]).items() if t - commodities.WINDOW_MAX_AGE <= T <= t}
    wb = commodities.window_basis(refs, minutes, t)
    if wb is None:
        return "under 6 boundaries to learn the perp's offset from"
    hours = max(float(b["close_ts"]) - t, 0.0) / 3600.0
    try:
        sigma = commodities.realized_sigma(feed, hours + age / 3600.0) * s.commodity_vol_mult
    except commodities.CommodityError:
        return "under 20 five-minute bars of the perp before the bet"
    unc = max(wb.sd, s.commodity_window_unc_floor)
    center = vs[i] * math.exp(wb.basis)
    p_yes = commodities.prob_above(center, float(b["strike"]), math.sqrt(sigma * sigma + unc * unc))
    p_yes = min(max(p_yes, s.commodity_tail_floor), 1.0 - s.commodity_tail_floor)
    p = p_yes if b["side"] == "yes" else 1.0 - p_yes
    shape = None
    a = bisect.bisect_left(ts, t - 34 * 60 - 60)
    grid = live_grid(ts[a : i + 1], vs[a : i + 1], t, 34 * 60)
    if grid:
        shape = features_curvature(grid[0], grid[1], grid[2], math.log(float(b["strike"])) - wb.basis, 1 if b["side"] == "yes" else -1)
    return p, edge_of(p, float(b["ask"])), (shape[:6] if shape else None)


def line(rows):
    stake = sum(float(r[0]["stake_usd"]) for r in rows)
    tot = sum(r[1] for r in rows)
    return f"n={len(rows):4d} | {tot:+8.2f} on ${stake:8.2f} ({(tot / stake if stake else 0):+.1%}) | {sum(1 for r in rows if r[1] > 0)} ended up"


priced, why_not = [], collections.defaultdict(list)
for b in bets:
    got = price(b)
    d = dollars(b)
    if isinstance(got, str):
        why_not[got].append((b, d))
        continue
    bar = getattr(specs.get(b["lane"]), "min_edge", None)
    priced.append((b, d, got[0], got[1], got[2], 0.015 if bar is None else bar))

print(f"\nall {len(bets)} bets as they happened: {line([(b, dollars(b)) for b in bets])}")
print("bets the ladder could not price from the perp (left out):")
for why, rows in sorted(why_not.items(), key=lambda kv: -len(kv[1])):
    print(f"   {why:55s} {line(rows)}")
if not priced:
    print("no bet could be priced")
    raise SystemExit(0)
print(f"\nPRICED bets (first {hm(float(priced[0][0]['ts']))} ET), WHAT HAPPENED: {line(priced)}")
kept = [r for r in priced if r[3] >= r[5]]
skipped = [r for r in priced if r[3] < r[5]]
change = -sum(r[1] for r in skipped)
beat, lost = sum(1 for r in skipped if r[1] < 0), sum(1 for r in skipped if r[1] > 0)
print(f"   kept (edge >= the lane's min_edge)   {line(kept)}")
print(f"   skipped                              {line(skipped)}")
print(f"   net change against what happened: {change:+.2f} | bets the rule beat (skipped losers) {beat}, lost to (skipped winners) {lost}, same {len(priced) - beat - lost}")

print("\nby lane: all priced | kept")
for name in sorted({r[0]["lane"] for r in priced}):
    print(f"   {name:26s} {line([r for r in priced if r[0]['lane'] == name])}  ||  {line([r for r in kept if r[0]['lane'] == name])}")

print("\nby the ladder's edge (chance - ask - fee): n | won share | mean ask | mean ladder chance | as booked | held to settlement")
for lo, hi in ((-9, -0.15), (-0.15, -0.05), (-0.05, 0.015), (0.015, 0.05), (0.05, 9)):
    g = [r for r in priced if lo <= r[3] < hi]
    if g:
        won = sum(1 for r in g if r[0]["result"] == r[0]["side"]) / len(g)
        print(f"   edge {lo:+.3f} to {hi:+.3f}: n={len(g):4d} | won {won:.1%} | ask {sum(float(r[0]['ask']) for r in g) / len(g):.3f} | chance {sum(r[2] for r in g) / len(g):.3f} | {sum(r[1] for r in g):+8.2f} | {sum(dollars(r[0], held=True) for r in g):+8.2f}")
y = [1.0 if r[0]["result"] == r[0]["side"] else 0.0 for r in priced]
for name, ps in (("the ask", [float(r[0]["ask"]) for r in priced]), ("the ladder chance", [r[2] for r in priced]), ("their average", [(float(r[0]["ask"]) + r[2]) / 2 for r in priced])):
    print(f"   Brier of {name:18s} {sum((p - o) ** 2 for p, o in zip(ps, y, strict=True)) / len(y):.4f}")

print("\nby curve shape (terciles of the perp's own readings at the buy, side bought): all priced | kept")
ok_shapes = True
shaped = [r for r in priced if r[4]]
for j, name in enumerate(SHAPE_NAMES):
    v = sorted(r[4][j] for r in shaped)
    if len(v) < 6:
        continue
    c1, c2 = v[len(v) // 3], v[2 * len(v) // 3]
    for label, pick in (("low ", lambda x: x < c1), ("mid ", lambda x: c1 <= x < c2), ("high", lambda x: x >= c2)):
        g = [r for r in shaped if pick(r[4][j])]
        k = [r for r in g if r[3] >= r[5]]
        ktot = sum(r[1] for r in k)
        flag = ""
        if len(k) >= 5 and ktot <= 0:
            ok_shapes, flag = False, "  <- kept bets not positive"
        print(f"   {name:10s} {label}: all n={len(g):4d} {sum(r[1] for r in g):+8.2f} | kept n={len(k):4d} {ktot:+8.2f}{flag}")
print(f"   (shape unread on {len(priced) - len(shaped)} priced bets: under 34 minutes of perp ticks)")

ktot = sum(r[1] for r in kept)
checks = [(f"kept at least 30 bets ({len(kept)})", len(kept) >= 30), (f"change against what happened positive ({change:+.2f})", change > 0), (f"kept bets net positive ({ktot:+.2f})", ktot > 0), ("kept bets positive in every shape tercile with 5+ kept", ok_shapes and len(kept) >= 5)]
print()
for text, ok in checks:
    print(f"   [{'x' if ok else ' '}] {text}")
print(f"VERDICT: the ladder buy from the Kalshi perp {'QUALIFIES for a shadow lane (the live switch stays yours)' if all(ok for _, ok in checks) else 'does NOT meet the bar on these bets'}.")

# ---- Part B: the curve's buy equation on its own bets ----
print("\nPART B: the curve model's predicted net per $1 (from the bet's note) against what its bets returned")
rows = []
for b in bets:
    m = PRED.search(str(b["reason"] or ""))
    if m:
        cost = float(b["stake_usd"])
        rows.append((float(m.group(1)), dollars(b, held=True) / cost, dollars(b) / cost, b))
print(f"   bets whose note carries a prediction: {len(rows)} of {len(bets)}")
if len(rows) >= 9:
    rows.sort(key=lambda r: r[0])
    n = len(rows)
    for label, g in (("lowest third ", rows[: n // 3]), ("middle third ", rows[n // 3 : 2 * n // 3]), ("highest third", rows[2 * n // 3 :])):
        won = sum(1 for r in g if r[3]["result"] == r[3]["side"]) / len(g)
        print(f"   {label}: n={len(g):4d} | predicted {g[0][0]:+.2f} to {g[-1][0]:+.2f} (mean {sum(r[0] for r in g) / len(g):+.3f}) | won {won:.1%} at mean ask {sum(float(r[3]['ask']) for r in g) / len(g):.3f} | held to settlement {sum(r[1] for r in g) / len(g):+.3f} per $1 | as booked {sum(r[2] for r in g) / len(g):+.3f} per $1")

    def ranks(xs):
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        out = [0.0] * len(xs)
        for rank, i in enumerate(order):
            out[i] = float(rank)
        return out

    def corr(a, b_):
        ma, mb = sum(a) / len(a), sum(b_) / len(b_)
        va, vb = sum((x - ma) ** 2 for x in a), sum((x - mb) ** 2 for x in b_)
        return sum((x - ma) * (z - mb) for x, z in zip(a, b_, strict=True)) / math.sqrt(va * vb) if va > 0 and vb > 0 else 0.0

    rp = ranks([r[0] for r in rows])
    print(f"   rank correlation of the prediction with the return: held to settlement {corr(rp, ranks([r[1] for r in rows])):+.3f}, as booked {corr(rp, ranks([r[2] for r in rows])):+.3f} (0 = the equation does not order its own bets)")
    print(f"   mean predicted {sum(r[0] for r in rows) / n:+.3f} per $1 against {sum(r[1] for r in rows) / n:+.3f} held to settlement and {sum(r[2] for r in rows) / n:+.3f} as booked")
    for lane in sorted({r[3]["lane"] for r in rows}):
        g = [r for r in rows if r[3]["lane"] == lane]
        if len(g) >= 9:
            rg = ranks([r[0] for r in g])
            print(f"   {lane:26s} n={len(g):4d} | rank correlation held {corr(rg, ranks([r[1] for r in g])):+.3f}, as booked {corr(rg, ranks([r[2] for r in g])):+.3f} | mean predicted {sum(r[0] for r in g) / len(g):+.3f}, held {sum(r[1] for r in g) / len(g):+.3f}, booked {sum(r[2] for r in g) / len(g):+.3f}")
print("DONE")
