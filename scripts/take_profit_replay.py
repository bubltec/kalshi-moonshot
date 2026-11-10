"""Take profit when the curve says the bid is richer than holding (2026-11-10, user: "we could definitely just take profit in some scenarios ... it can also sell at the right time").
Question: on the same live bets, is selling at the bid better than holding when the bid pays more than the curve's own fair value of the position, and how does that compare with the shock exit's loss-cutting?
Fair value of holding (digital, from the market's own curve): P = Phi(c * sqrt(60 / s)), where c is the spot's cushion over the reference (the strike for crypto, the window's open price for the others) signed with the bet and in typical minutes (rms one-second move of the last 900 s times sqrt(60), read from the market's own ticks), and s the seconds left. P is the chance the side settles at $1 if the spot moves like its own last 15 minutes. Selling a position at bid b nets b less the fee; holding is worth P.
Rule: with s seconds left, at most L, sell when the bid net of fee is at least P + m, and (profit_only) the bid is at least the entry price + 0.05. Persistence 0 or 5 s. Sale at the bid read that second, less the fee; counterfactual is holding to the result (the live exits are not replayed, so stacking on them is untested).
Variants (grid fixed before the results; the choice among them is made after seeing them and is said to be so): margin m 0.00 / 0.03 / 0.06 / 0.10; profit_only off / on; L 120 / 300 / 600 / 900; persistence 0 / 5 s.
Groups are the lanes' own buy rules (crypto, favorites, commodity_multiscale), never a market name. Each row: net change against holding, bets sold, winners sold / losers saved, whole-stake losses turned partial, two halves (median ts), curve shapes (terciles of the spot's path efficiency over the 5 minutes before the buy). A variant counts only when positive in both halves and in each shape it claims.

    scripts/cloud_job.sh scripts/take_profit_replay.py
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
        "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
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
        rate = math.sqrt((sq[k] - sq[k - 900]) / 900)
        if rate <= 0:
            continue
        unit = rate * math.sqrt(60.0)
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            continue
        yb, ya = float(q[i][1]), float(q[i][2])
        bid = yb if r["side"] == "yes" else 1.0 - ya
        reads.append((t, left, min(m for _, m in moves) / unit, (g[k] - ref) * sign / unit, bid))
    return eff, reads


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def decide(r, reads, eff_top, v):
    """(pnl, sold) under v = (margin, profit_only, L, persist)."""
    m, profit_only, L, persist = v
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    entry = cost / n
    since = None
    for t, left, wu, mu, bid in reads:
        if left > L or left < 8 or bid < 0.05:
            since = None
            continue
        p = phi(mu * math.sqrt(60.0 / left))
        ok = bid - fee(bid) >= p + m and (not profit_only or bid >= entry + 0.05)
        if not ok:
            since = None
            continue
        if since is None:
            since = t
        if t - since >= persist:
            return n * (bid - fee(bid)) - cost, True
    return hold, False


VARIANTS = [(m, po, L, ps) for m in (0.0, 0.03, 0.06, 0.10) for po in (False, True) for L in (120, 300, 600, 900) for ps in (0, 5)]

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
    zeros = [i for i, (r, _, _) in enumerate(data) if hold[i] <= -float(r["stake_usd"]) + 1e-9]
    print(f"  held to the result {sum(hold):+.2f} | whole-stake losses {len(zeros)} | shape cutoffs {lo:.3f} {hi:.3f}")
    out = []
    for v in VARIANTS:
        res = [decide(r, reads, False, v) for r, e, reads in data]
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
        out.append((sum(d), v, len(ch), saved, won, fixed, h1, h2, by))
    out.sort(key=lambda o: -o[0])
    print("  variants (margin, profit_only, L, persist), best 12 by change against holding:")
    for o in out[:12]:
        sh = " ".join(f"{k} {v:+.1f}" for k, v in sorted(o[8].items()))
        print(f"    {o[1]}: vs holding {o[0]:+.2f} on {o[2]} sold | losers saved {o[3]}, winners sold {o[4]}, zeros turned partial {o[5]} of {len(zeros)} | halves {o[6]:+.2f} / {o[7]:+.2f} | {sh}")
    both = [o for o in out if o[6] > 0 and o[7] > 0]
    print(f"  positive in both halves: {len(both)} of {len(out)}")
    for o in sorted(both, key=lambda o: -o[0])[:8]:
        sh = " ".join(f"{k} {v:+.1f}" for k, v in sorted(o[8].items()))
        print(f"    {o[1]}: vs holding {o[0]:+.2f} on {o[2]} sold | winners sold {o[4]}, zeros partial {o[5]} | halves {o[6]:+.2f} / {o[7]:+.2f} | {sh}")
print("DONE")
