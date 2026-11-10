"""Does reading the exit's typical minute against the market's own regime beat the trailing-15-minute reading? (2026-11-10, user: "the changing market and adapting the curve to it in real time ... minimize loss and maximize profits")
Observation (11-10, cloud tape): spot was calm 12:15-13:25 UTC (BTC typical minute about 4 bp, its 24 h median), then at about 13:27 it jumped to about 3x. The live shock exit reads the rms one-second move of the last 900 s, so after a calm stretch a 10 bp wiggle reads as 2+ units and sells winners (11-10 13:10: ETH, ZEC, gold sold at 0.25-0.54 and won), and in a burst the window is still calm for the first minutes. The crypto lane also has no shock_persist.
Question: on the same live bets, does the exit do better when its unit (one typical minute) is read at more than one scale, and does the burst/calm state at the decision carry information about whether to buy or to sell?
Unit readings (all rms one-second log move times sqrt(60), from the index's own ticks, no fixed numbers):
  u15   the last 900 s (what is live)
  u60   the last 3600 s (the market's own recent hour; needs 1800 s of ticks)
  ush   the last 180 s (a burst shows here first)
  regime ratio = ush / u60. 1 is a normal minute for that hour. Its cutoffs come from this group's own readings (the 90th percentile of the ratio over all decision seconds; hindsight over the sample, a live version would take each market's last hours).
Unit modes for the exit: "u15" (live); "floor" = max(u15, u60) so a calm stretch cannot shrink the cutoff below the hour's own typical minute; "burst" = max(u15, ush) so a burst scales the cutoff at once; "both" = max of the three.
Exit variants (grid fixed before the results; choice made after seeing them, said to be so): unit mode x persistence 0 / 20 s x regime sit-out off / on (no exit while the ratio is at or above its 90th percentile: anomaly state, rule 3). Size, cushion and the window follow each group's live setting.
Baseline "live rule": the group's live size/cushion/persistence on u15. Counterfactual is holding to the result; the actual live result (the lane's own sale where one happened, including the value exit) is printed for reference but the replay does not model the value exit.
Entry: held-to-result net per dollar staked of the bets bought in a calm (ratio below its 25th percentile), normal and burst (at or above its 90th percentile) regime, in two halves: only a description, no rule here.
A variant counts only when positive against the live rule in both halves and in each curve shape it claims.

    scripts/cloud_job.sh scripts/regime_exit_replay.py
"""

import bisect
import calendar
import collections
import itertools
import math
import os

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)

# lanes, live window L, live size, live cushion, live persistence, since
GROUPS = {
    "crypto": (("crypto_multiscale", "crypto_multiscale_underdog"), 300.0, 2.0, 0.6, 0, int(os.environ.get("SINCE_CRYPTO", "1794000000"))),
    "commodity_multiscale": (("commodity_multiscale",), 180.0, 1.5, 0.3, 20, calendar.timegm((2026, 11, 9, 14, 0, 0))),
    "favorites": (("commodity_energy", "commodity_wti", "commodity_precious", "commodity_industrial"), 180.0, 1.5, 0.3, 20, calendar.timegm((2026, 11, 3, 0, 0, 0))),
}
MEM, SEC = 120, 60
BAND = (0.05, 0.89)
LONG, SHORT = 3600, 180


def load(lanes, since):
    marks = ",".join("?" * len(lanes))
    rows = st.db.execute(
        "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,d.exit_price,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
        f"WHERE d.action='bet' AND d.mode='trade' AND d.contracts>0 AND d.stake_usd>0 AND d.ts>? AND d.lane IN ({marks}) AND w.result IN ('yes','no') ORDER BY d.ts",
        (since, *lanes),
    ).fetchall()
    seen = {}
    for r in rows:
        seen[(r["ticker"], r["side"])] = r
    return list(seen.values())


def prepare(r):
    """(path efficiency at the buy, regime ratio at the buy, per-second readings from the buy to the close)."""
    ser = r["ticker"].split("-")[0]
    crypto = variants.crypto_index(ser)
    ix = crypto or variants.biquote_index(ser)
    if not ix:
        return None
    close, t0 = float(r["close_ts"]), float(r["ts"])
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, t0 - LONG - 60, close)).fetchall()
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], t0 - 5, close)).fetchall()
    if len(tk) < 2000 or len(q) < 5:
        return None
    T = [float(x[0]) for x in tk]
    V = [float(x[1]) for x in tk]
    if crypto:
        if not r["strike"] or float(r["strike"]) <= 0:
            return None
        ref = math.log(float(r["strike"]))
    else:
        opened = close - 900.0
        a = bisect.bisect_left(T, opened - 5.0)
        if a >= len(T) or T[a] > opened + 20.0:
            return None
        ref = math.log(V[a])
    base = int(T[0])
    g, j, last = [], 0, None
    for t in range(base, int(close) + 1):
        while j < len(T) and T[j] <= t:
            last = V[j]
            j += 1
        g.append(math.log(last) if last else None)
    sq = [0.0]
    for x, y in itertools.pairwise(g):
        sq.append(sq[-1] + ((y - x) ** 2 if x is not None and y is not None else 0.0))

    def unit(k, w):
        w = min(w, k)
        if w < min(w, 1800) or w < 120:
            return None
        v = (sq[k] - sq[k - w]) / w
        return math.sqrt(v) * math.sqrt(60.0) if v > 0 else None

    kb = int(t0) - base
    seg = [x for x in g[max(kb - 300, 0) : kb + 1] if x is not None]
    path = sum(abs(y - x) for x, y in itertools.pairwise(seg))
    eff = abs(seg[-1] - seg[0]) / path if len(seg) > 30 and path > 0 else None
    sign = 1.0 if r["side"] == "yes" else -1.0
    ub, ul = unit(kb, SHORT), unit(kb, LONG)
    ratio_buy = ub / ul if ub and ul else None
    qt = [float(x[0]) for x in q]
    moves = collections.deque()
    reads = []
    for t in range(int(t0), int(close) - 5):
        k = t - base
        if k < 1800 or k >= len(g) or g[k] is None or g[k - SEC] is None:
            continue
        moves.append((t, (g[k] - g[k - SEC]) * sign))
        while moves and t - moves[0][0] > MEM:
            moves.popleft()
        left = close - t
        if left > 330:
            continue
        u15, ush, u60 = unit(k, 900), unit(k, SHORT), unit(k, LONG)
        if not u15 or not ush or not u60:
            continue
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            continue
        yb, ya = float(q[i][1]), float(q[i][2])
        bid = yb if r["side"] == "yes" else 1.0 - ya
        reads.append((t, left, min(m for _, m in moves), (g[k] - ref) * sign, bid, u15, ush, u60))
    return eff, ratio_buy, reads


def decide(r, reads, v, hi_ratio):
    """(pnl, sold) under v = (mode, size, cushion, persist, sit_out, L)."""
    mode, size, cush, persist, sit_out, L = v
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    since = None
    for t, left, mv, cu, bid, u15, ush, u60 in reads:
        u = u15 if mode == "u15" else max(u15, u60) if mode == "floor" else max(u15, ush) if mode == "burst" else max(u15, u60, ush)
        ok = left <= L and BAND[0] <= bid <= BAND[1] and cu / u <= cush and mv / u <= -size and not (sit_out and ush / u60 >= hi_ratio)
        if not ok:
            since = None
            continue
        if since is None:
            since = t
        if t - since >= persist:
            return n * (bid - fee(bid)) - cost, True
    return hold, False


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else 0.0


for gname, (lanes, own_L, size0, cush0, pers0, since) in GROUPS.items():
    bets = load(lanes, since)
    data = []
    for r in bets:
        got = prepare(r)
        if got:
            data.append((r, *got))
    print(f"\n=== {gname}: settled live bets {len(bets)} | replayed {len(data)}")
    if len(data) < 20:
        print("  too few bets")
        continue
    effs = sorted(e for _, e, _, _ in data if e is not None)
    lo, hi = effs[len(effs) // 3], effs[2 * len(effs) // 3]
    mid = sorted(float(r["ts"]) for r, *_ in data)[len(data) // 2]
    allr = [x[6] / x[7] for _, _, _, reads in data for x in reads]
    hi_ratio, lo_ratio = pct(allr, 0.90), pct([rb for _, _, rb, _ in data if rb], 0.25)
    print(f"  regime ratio ush/u60: 90th pct over decision seconds {hi_ratio:.2f}; 25th pct at the buy {lo_ratio:.2f}")

    def shape(e):
        return "unknown" if e is None else "choppy" if e <= lo else "straight" if e >= hi else "middle"

    hold = [((float(r["contracts"]) if r["result"] == r["side"] else 0.0) - float(r["stake_usd"])) for r, *_ in data]
    actual = [((float(r["contracts"]) * float(r["exit_price"]) - float(r["stake_usd"])) if r["exit_price"] else h) for (r, *_), h in zip(data, hold, strict=True)]
    print(f"  held to the result {sum(hold):+.2f} | actual live (own sales, no fee on sales) {sum(actual):+.2f}")
    live = (("u15", size0, cush0, pers0, False, own_L))
    cur = [decide(r, reads, live, hi_ratio) for r, _, _, reads in data]
    print(f"  live rule replayed {live}: net {sum(p for p, _ in cur):+.2f}, sold {sum(f for _, f in cur)}, against holding {sum(p for p, _ in cur) - sum(hold):+.2f}")

    out = []
    for mode in ("u15", "floor", "burst", "both"):
        for persist in (0, 20):
            for sit_out in (False, True):
                v = (mode, size0, cush0, persist, sit_out, own_L)
                res = [decide(r, reads, v, hi_ratio) for r, _, _, reads in data]
                d = [p - c[0] for (p, _), c in zip(res, cur, strict=True)]
                ch = [(x, i) for i, x in enumerate(d) if abs(x) > 1e-9]
                if not ch:
                    continue
                h1 = sum(x for x, i in ch if float(data[i][0]["ts"]) <= mid)
                h2 = sum(x for x, i in ch if float(data[i][0]["ts"]) > mid)
                by = {}
                for x, i in ch:
                    by[shape(data[i][1])] = by.get(shape(data[i][1]), 0.0) + x
                saved = sum(1 for (p, f), h in zip(res, hold, strict=True) if f and h < 0)
                sold_w = sum(1 for (p, f), h in zip(res, hold, strict=True) if f and h > 0)
                out.append((sum(d), v, sum(p for p, _ in res), len(ch), saved, sold_w, sum(f for _, f in res), h1, h2, by))
    out.sort(key=lambda o: -o[0])
    print("  exit variants (mode, size, cushion, persist, sit_out, L) against the live rule:")
    for o in out:
        sh = " ".join(f"{k} {v:+.1f}" for k, v in sorted(o[9].items()))
        print(f"    {o[1]}: net {o[2]:+.2f} | vs live {o[0]:+.2f} on {o[3]} changed, {o[6]} sold | losers saved {o[4]}, winners sold {o[5]} | halves {o[7]:+.2f} / {o[8]:+.2f} | {sh}")
    print(f"  positive in both halves: {sum(1 for o in out if o[7] > 0 and o[8] > 0)} of {len(out)}")

    print("  entry regime (ratio at the buy), held to the result, net per $ staked:")
    for name, test in (("calm  (< 25th pct)", lambda x: x is not None and x < lo_ratio), ("normal", lambda x: x is not None and lo_ratio <= x < hi_ratio), ("burst (>= 90th pct)", lambda x: x is not None and x >= hi_ratio)):
        idx = [i for i, (_, _, rb, _) in enumerate(data) if test(rb)]
        if not idx:
            print(f"    {name}: none")
            continue
        a = [i for i in idx if float(data[i][0]["ts"]) <= mid]
        b = [i for i in idx if float(data[i][0]["ts"]) > mid]

        def per(ii):
            stake = sum(float(data[i][0]["stake_usd"]) for i in ii)
            return f"{sum(hold[i] for i in ii) / stake:+.3f}/$ n={len(ii)}" if ii else "-"

        print(f"    {name}: {per(idx)} | halves {per(a)} / {per(b)} | whole-stake losses {sum(1 for i in idx if hold[i] <= -float(data[i][0]['stake_usd']) + 1e-9)}")
print("DONE")
