"""Does persistence help the crypto shock exit at any length? (2026-11-10, user: "crypto persistence")
Question: on the same live crypto bets, does requiring the shock exit's whole condition to hold for N consecutive seconds (N 3 / 5 / 10 / 15 / 20) beat selling on the first reading, at the live size or a larger one?
Why again: 2026-11-09's replay found no crypto variant that held up, and 2026-11-10's regime replay (studies/results/2026-11-10-regime-exit.txt) found 20 s persistence worse than live on crypto (-$11 to -$20 against the live rule). Neither tried short persistence: crypto gaps in seconds, so 20 s may be too long while 3-10 s filters a one-tick spike.
Population: crypto_multiscale and crypto_multiscale_underdog live bets (since SINCE_CRYPTO), one row per market and side, settled window, held to the result as the counterfactual (the value exit and the real sales are not replayed). The index is read against the strike.
Live rule (baseline): in the last 300 s, the worst 60 s move against the bet over the last 120 s is at least 2.0 typical minutes (rms one-second log move over 900 s times sqrt(60)), the cushion over the strike is at most 0.6 typical minutes, held-side bid in [0.05, 0.89], no persistence. Sale at the bid less the fee.
Variants (grid fixed before the results; the choice among them is made after seeing them and is said to be so): persistence 0 / 3 / 5 / 10 / 15 / 20 s x size 1.5 / 2.0 / 2.5 / 3.0 typical minutes x cushion 0.6 / 0.3 x last 300 / 180 s.
Each row: net, change against holding and against the live rule, bets changed, losers saved / winners sold, the two halves, and curve shapes (path efficiency terciles). A variant counts only when positive against the live rule in both halves and in each shape it claims.

    scripts/cloud_job.sh scripts/crypto_persistence_replay.py
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
        if left > 330:
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


def decide(r, reads, eff_top, v):
    """(pnl, sold) of the bet under variant v = (size, cushion, persist, shape_gate, cross, L)."""
    size, cush, persist, shape_gate, cross, L = v
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    if shape_gate and eff_top:
        return hold, False
    since = None
    for t, left, wu, mu, bid in reads:
        ok = left <= L and BAND[0] <= bid <= BAND[1] and mu <= cush and (cross or wu <= -size)
        if not ok:
            since = None
            continue
        if since is None:
            since = t
        if t - since >= persist:
            return n * (bid - fee(bid)) - cost, True
    return hold, False


VARIANTS = []
for size in (1.5, 2.0, 2.5, 3.0):
    for cush in (0.6, 0.3):
        for persist in (0, 3, 5, 10, 15, 20):
            VARIANTS.append((size, cush, persist, False, False))


def ms(xs):
    return f"{sum(xs):+.2f} (n={len(xs)})"


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
    times = sorted(float(r["ts"]) for r, _, _ in data)
    mid = times[len(times) // 2]

    def shape(e):
        return "unknown" if e is None else "choppy" if e <= lo else "straight" if e >= hi else "middle"

    hold = [((float(r["contracts"]) if r["result"] == r["side"] else 0.0) - float(r["stake_usd"])) for r, _, _ in data]
    print(f"  held to the result {sum(hold):+.2f} | shape cutoffs (path efficiency) {lo:.3f} {hi:.3f}")
    cur = [decide(r, reads, False, (2.0, 0.6, 0, False, False, own_L)) for r, _, reads in data]
    print(f"  live rule (2.0 / 0.6 / L={own_L:.0f}): net {sum(p for p, _ in cur):+.2f}, sold {sum(f for _, f in cur)}, against holding {sum(p for p, _ in cur) - sum(hold):+.2f}")
    out = []
    for v in VARIANTS:
        for L in (own_L, 180.0):
            full = (*v, L)
            res = [decide(r, reads, shape(e) == "straight", full) for r, e, reads in data]
            d = [p - c[0] for (p, _), c in zip(res, cur, strict=True)]
            changed = [(x, i) for i, x in enumerate(d) if abs(x) > 1e-9]
            if not changed:
                continue
            h1 = sum(x for x, i in changed if float(data[i][0]["ts"]) <= mid)
            h2 = sum(x for x, i in changed if float(data[i][0]["ts"]) > mid)
            by = {}
            for x, i in changed:
                by[shape(data[i][1])] = by.get(shape(data[i][1]), 0.0) + x
            saved = sum(1 for (p, f), h in zip(res, hold, strict=True) if f and h < 0)
            sold_w = sum(1 for (p, f), h in zip(res, hold, strict=True) if f and h > 0)
            out.append((sum(d), full, sum(p for p, _ in res), len(changed), saved, sold_w, h1, h2, by))
    out.sort(key=lambda o: -o[0])
    print("  variants against the live rule (size, cushion, persist, shape_gate, cross, L), best 20 by change:")
    for o in out[:20]:
        sh = " ".join(f"{k} {v:+.1f}" for k, v in sorted(o[8].items()))
        print(f"    {o[1]}: net {o[2]:+.2f} | vs current {o[0]:+.2f} on {o[3]} changed | losers saved {o[4]}, winners sold {o[5]} | halves {o[6]:+.2f} / {o[7]:+.2f} | {sh}")
    both = [o for o in out if o[6] > 0 and o[7] > 0]
    print(f"  positive in both halves: {len(both)} of {len(out)} variants")
print("DONE")
