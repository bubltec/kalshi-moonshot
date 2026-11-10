"""One curve exit with spike and shock built in, no named exit rules (2026-11-10, user: "i want to get rid of all the rigid exit ideas. it should be based on curve with spike and shock concepts built in").
Idea: a held position is worth the chance its side settles at $1. The curve gives that chance from the spot's cushion over the reference (the strike for crypto, the window's open price for the others) and the time left, in the market's own variance: P_w = Phi(c / (r_w * sqrt(s))), c the signed cushion in log, r_w the rms one-second log move over the last w seconds, s the seconds left. Selling pays the bid less the fee. One rule: sell when the bid net of fee is at least the PESSIMISTIC curve value of holding plus a margin m, P = min over scales w of P_w.
Spike and shock are inside the scales, not separate rules: a spike (a burst of movement) raises the short-scale r_w at once, which pulls P toward even and lets the bid beat it; a shock (a fast move against the bet) shrinks the cushion c the same way. A quiet market has all scales alike and the rule rarely fires. The same rule takes profit (a bid richer than the curve's value of holding) and cuts losses (a bid above what a position through the strike is worth). Nothing is keyed on a market, a price or a fixed number of basis points.
Variants (grid fixed before the results; the choice among them is made after seeing them and is said to be so): scale sets {900} / {900, 120} / {900, 300, 120, 60} seconds; margin m 0.00 / 0.03 / 0.06 / 0.10 / 0.15; persistence 0 / 5 s; window L: any time left / last 300 s. Sale at the bid read that second, less the fee; counterfactual is holding to the result; the live result (its own sales, which stack the shock, late-drop, strike-through and value exits) is printed for the same bets. Groups are the lanes' own buy rules; a variant counts only when positive against holding in both halves and in each curve shape it claims.

    scripts/cloud_job.sh scripts/curve_exit_unified_replay.py
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
    for t in range(int(t0), int(close) - 5):
        k = t - base
        if k < 960 or k >= len(g) or g[k] is None or g[k - SEC] is None:
            continue
        moves.append((t, (g[k] - g[k - SEC]) * sign))
        while moves and t - moves[0][0] > MEM:
            moves.popleft()
        left = close - t
        if left > 900:
            continue
        rates = tuple(math.sqrt((sq[k] - sq[k - w]) / w) for w in (900, 300, 120, 60))
        if rates[0] <= 0 or min(rates) < 0:
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


SCALES = {"900": (0,), "900+120": (0, 2), "900+300+120+60": (0, 1, 2, 3)}


def decide(r, reads, v):
    """(pnl, sold) under v = (scales, margin, persist, L)."""
    sc, m, persist, L = v
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    since = None
    for t, left, c, rates, bid in reads:
        if left > L or left < 8 or bid < 0.05:
            since = None
            continue
        p = min(phi(c / (rates[j] * math.sqrt(left))) if rates[j] > 0 else (1.0 if c > 0 else 0.0) for j in SCALES[sc])
        if bid - fee(bid) >= p + m:
            if since is None:
                since = t
            if t - since >= persist:
                return n * (bid - fee(bid)) - cost, True
        else:
            since = None
    return hold, False


VARIANTS = [(sc, m, ps, L) for sc in SCALES for m in (0.0, 0.03, 0.06, 0.10, 0.15) for ps in (0, 5) for L in (900, 300)]

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
    print("  variants (scales, margin, persist, L), best 12 by net, then the ones positive against holding in both halves:")
    for o in out[:12]:
        sh = " ".join(f"{k} {v:+.1f}" for k, v in sorted(o[9].items()))
        print(f"    {o[2]}: net {o[0]:+.2f} (vs holding {o[1]:+.2f}, vs live {o[0] - sum(live):+.2f}) on {o[3]} sold | losers saved {o[4]}, winners sold {o[5]}, zeros partial {o[6]} of {len(zeros)} | halves {o[7]:+.2f} / {o[8]:+.2f} | {sh}")
    both = [o for o in out if o[7] > 0 and o[8] > 0]
    print(f"  positive against holding in both halves: {len(both)} of {len(out)}")
    for o in sorted(both, key=lambda o: -o[1])[:8]:
        sh = " ".join(f"{k} {v:+.1f}" for k, v in sorted(o[9].items()))
        print(f"    {o[2]}: vs holding {o[1]:+.2f}, vs live {o[0] - sum(live):+.2f} on {o[3]} sold | winners sold {o[5]}, zeros partial {o[6]} | halves {o[7]:+.2f} / {o[8]:+.2f} | {sh}")
print("DONE")
