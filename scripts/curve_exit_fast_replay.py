"""A curve exit that reacts at once to a collapse (2026-11-10, user: "it dropped in the last 10s but the curve logic should be picking up every tick"; KXPALLADIUM15M-26NOV101430 YES bought at 0.90, spot 1032.25 -> 1031.68 in one second at 8 s left: the curve chance went 0.80 -> 0.00 on that tick while the bid was still 0.69, then 0.38 a second later.)
Question: the curve exit (scripts/curve_exit_unified_replay_v2.py, live on commodity_multiscale and the underdog lane) holds a condition 5 s and does not act in the last 8 s, so a one-tick collapse is not sold. Does selling at once when the bid is far over the curve's chance, and acting until 3 s before the close, beat the 5 s persistence, on the same live bets?
Reading and rule as v2: P = the lower Phi(cushion / (rate * sqrt(seconds left))) over the rates; sell when the bid net of the sell fee is at least P + margin. New here: a fast-over level F: when the bid net is at least P + F the sale does not wait for the persistence; and the last-seconds guard 8 s (live) or 3 s (the shock exit's). A sale fills at the NEXT second's bid (about one second of latency), less the fee; held to the result is the counterfactual; the live stacked result is printed.
Variants (grid fixed before the results; the choice is made after seeing them and is said to be so): scales 60s900+60s180 / 60s900; margin 0.06 / 0.15; persistence 0 / 5 s; fast-over F none / 0.25 / 0.40; guard 8 / 3 s; any time left in the last 900 s. A variant counts only when positive against holding in both halves and in each curve shape it claims.

    scripts/cloud_job.sh scripts/curve_exit_fast_replay.py
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

GROUPS = {
    "crypto": (("crypto_multiscale", "crypto_multiscale_underdog"), 300.0, int(os.environ.get("SINCE_CRYPTO", "1794000000"))),
    "commodity_multiscale": (("commodity_multiscale",), 300.0, calendar.timegm((2026, 11, 9, 14, 0, 0))),
    "favorites": (("commodity_energy", "commodity_wti", "commodity_precious", "commodity_industrial"), 120.0, calendar.timegm((2026, 11, 3, 0, 0, 0))),
}
MEM, SEC = 120, 60
BAND = (0.05, 0.89)


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
    """The per-second readings the exit sees from the buy to the close, plus the curve shape at the buy."""
    ser = r["ticker"].split("-")[0]
    crypto = variants.crypto_index(ser)
    ix = crypto or variants.biquote_index(ser)
    if not ix:
        return None
    close, t0 = float(r["close_ts"]), float(r["ts"])
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, t0 - 1500, close)).fetchall()
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], t0 - 5, close)).fetchall()
    if len(tk) < 1000 or len(q) < 5:
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
    c60 = [0.0] * len(g)
    for j in range(60, len(g)):
        c60[j] = c60[j - 1] + (((g[j] - g[j - 60]) ** 2) / 60.0 if g[j] is not None and g[j - 60] is not None else 0.0)
    sq = [0.0]
    for x, y in itertools.pairwise(g):
        sq.append(sq[-1] + ((y - x) ** 2 if x is not None and y is not None else 0.0))
    kb = int(t0) - base
    seg = [x for x in g[max(kb - 300, 0) : kb + 1] if x is not None]
    path = sum(abs(y - x) for x, y in itertools.pairwise(seg))
    eff = abs(seg[-1] - seg[0]) / path if len(seg) > 30 and path > 0 else None
    sign = 1.0 if r["side"] == "yes" else -1.0
    qt = [float(x[0]) for x in q]
    moves = collections.deque()
    reads = []
    for t in range(int(t0), int(close) - 2):
        k = t - base
        if k < 960 or k >= len(g) or g[k] is None or g[k - SEC] is None:
            continue
        moves.append((t, (g[k] - g[k - SEC]) * sign))
        while moves and t - moves[0][0] > MEM:
            moves.popleft()
        left = close - t
        if left > 900:
            continue
        rates = (math.sqrt((sq[k] - sq[k - 900]) / 900), math.sqrt((c60[k] - c60[k - 900]) / 900), math.sqrt((c60[k] - c60[k - 180]) / 180))
        if rates[0] <= 0 or rates[1] <= 0 or rates[2] < 0:
            continue
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            continue
        yb, ya = float(q[i][1]), float(q[i][2])
        bid = yb if r["side"] == "yes" else 1.0 - ya
        reads.append((t, left, (g[k] - ref) * sign, rates, bid))
    return eff, reads


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


SCALES = {"60s900": (1,), "60s900+60s180": (1, 2)}


def decide(r, reads, v):
    """(pnl, sold) under v = (scales, margin, persist, fast, guard)."""
    sc, m, persist, fast, guard = v
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    since = None
    for idx, (t, left, c, rates, bid) in enumerate(reads):
        if left > 900 or left < guard or bid < 0.05:
            since = None
            continue
        p = min(phi(c / (rates[j] * math.sqrt(left))) if rates[j] > 0 else (1.0 if c > 0 else 0.0) for j in SCALES[sc])
        over = bid - fee(bid) - p
        if over >= m:
            if since is None:
                since = t
            if (fast is not None and over >= fast) or t - since >= persist:
                if idx + 1 >= len(reads):
                    continue
                sell = reads[idx + 1][4]
                if sell <= 0.0:
                    continue
                return n * (sell - fee(sell)) - cost, True
        else:
            since = None
    return hold, False


VARIANTS = [(sc, m, ps, fa, gd) for sc in SCALES for m in (0.06, 0.15) for ps in (0, 5) for fa in (None, 0.25, 0.40) for gd in (8, 3)]

for gname, (lanes, own_L, since) in GROUPS.items():
    bets = load(lanes, since)
    data = []
    for r in bets:
        got = prepare(r)
        if got:
            data.append((r, got[0], got[1]))
    print(f"\n=== {gname}: lanes {lanes} | settled live bets {len(bets)} | replayed {len(data)}")
    if len(data) < 20:
        print("  too few bets")
        continue
    effs = sorted(e for _, e, _ in data if e is not None)
    lo, hi = effs[len(effs) // 3], effs[2 * len(effs) // 3]
    mid = sorted(float(r["ts"]) for r, _, _ in data)[len(data) // 2]

    def shape(e):
        return "unknown" if e is None else "choppy" if e <= lo else "straight" if e >= hi else "middle"

    hold = [((float(r["contracts"]) if r["result"] == r["side"] else 0.0) - float(r["stake_usd"])) for r, _, _ in data]
    live = [((float(r["contracts"]) * float(r["exit_price"]) - float(r["stake_usd"])) if r["exit_price"] else h) for (r, _, _), h in zip(data, hold, strict=True)]
    zeros = [i for i, (r, _, _) in enumerate(data) if hold[i] <= -float(r["stake_usd"]) + 1e-9]
    print(f"  held to the result {sum(hold):+.2f} | live (own sales, stacked exits) {sum(live):+.2f} | whole-stake losses held {len(zeros)} | shape cutoffs {lo:.3f} {hi:.3f}")
    out = []
    for v in VARIANTS:
        res = [decide(r, reads, v) for r, e, reads in data]
        net = sum(p for p, _ in res)
        d = [p - h for (p, _), h in zip(res, hold, strict=True)]
        ch = [(x, i) for i, x in enumerate(d) if abs(x) > 1e-9]
        if not ch:
            continue
        h1 = sum(x for x, i in ch if float(data[i][0]["ts"]) <= mid)
        h2 = sum(x for x, i in ch if float(data[i][0]["ts"]) > mid)
        by = {}
        for x, i in ch:
            by[shape(data[i][1])] = by.get(shape(data[i][1]), 0.0) + x
        won = sum(1 for (p, f), h in zip(res, hold, strict=True) if f and h > 0)
        saved = sum(1 for (p, f), h in zip(res, hold, strict=True) if f and h < 0)
        fixed = sum(1 for i in zeros if res[i][1])
        out.append((net, sum(d), v, len(ch), saved, won, fixed, h1, h2, by))
    out.sort(key=lambda o: -o[0])
    print("  variants (scales, margin, persist, fast, guard), best 12 by net, then the ones positive against holding in both halves:")
    for o in out[:12]:
        sh = " ".join(f"{k} {v:+.1f}" for k, v in sorted(o[9].items()))
        print(f"    {o[2]}: net {o[0]:+.2f} (vs holding {o[1]:+.2f}, vs live {o[0] - sum(live):+.2f}) on {o[3]} sold | losers saved {o[4]}, winners sold {o[5]}, zeros partial {o[6]} of {len(zeros)} | halves {o[7]:+.2f} / {o[8]:+.2f} | {sh}")
    both = [o for o in out if o[7] > 0 and o[8] > 0]
    print(f"  positive against holding in both halves: {len(both)} of {len(out)}")
    for o in sorted(both, key=lambda o: -o[1])[:8]:
        sh = " ".join(f"{k} {v:+.1f}" for k, v in sorted(o[9].items()))
        print(f"    {o[2]}: vs holding {o[1]:+.2f}, vs live {o[0] - sum(live):+.2f} on {o[3]} sold | winners sold {o[5]}, zeros partial {o[6]} | halves {o[7]:+.2f} / {o[8]:+.2f} | {sh}")
print("DONE")
