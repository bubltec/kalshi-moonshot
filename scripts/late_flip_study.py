"""Late flips: why a bought favourite loses everything (2026-11-08, user: "I am still seeing a lot of total losses", after KXETH15M-26NOV080430 sat at 0.10 for five minutes and settled the other way).

Question: on every settled crypto 15-minute window, a market side whose ask is 0.85-0.97 with a given number of minutes left: how often does it lose everything, and does the spot's own curve tell the safe ones from the ones that flip? Read in the market's own variance: z = cushion (the spot's distance from the strike on that side) over what the seconds left can move at the last 13 minutes' one-second rate; the 13 minute slope of the cushion in typical moves.
A. Base rate by minutes left (first moment per market side at each 30 s step from minute 1 to 14.5 with ask 0.85-0.97, spread 0.04 or less): n, won vs break-even, net per $1 after the full taker fee held to the result, losers, halves of the period.
B. By z band at 3 to 9 minutes left (the zone where the live favourite lanes buy): same columns.
C. By 13 minute slope band at 3 to 9 minutes left.
D. The live favourite bets since 11-02 (lane buys at ask 0.80 or more): each bet's z and slope at the buy, whether it lost all, so the rule can be read against what happened to the bets.
Not covered: exits (shock, cross, take-profit) that sell before the result; this is held to the result. Cutoffs are read here as a grid, not chosen as rules.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/late_flip_study.py
"""

import bisect
import calendar
import itertools
import math
import os

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
t0 = calendar.timegm((2026, 11, 2, 6, 0, 0))
CRYPTO = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
wins = st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND series IN (?,?,?,?,?) AND result IN ('yes','no') AND strike>0 ORDER BY series,close_ts", (t0, *CRYPTO)).fetchall()
G0 = int(t0 - 4300)
G1 = int(max(float(w["close_ts"]) for w in wins)) + 2
L13 = 13 * 60
rows = []  # (close, left, ask, won, z, s13, ticker, side)
GRIDS = {}


def grid(ix):
    r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, G0 - 60)).fetchall()
    ts = [float(x[0]) for x in r]
    vs = [float(x[1]) for x in r]
    g, j, last = [], 0, None
    for t in range(G0, G1):
        while j < len(ts) and ts[j] <= t:
            last = vs[j]
            j += 1
        g.append(math.log(last) if last else None)
    sq = [0.0]
    for a, b in itertools.pairwise(g):
        sq.append(sq[-1] + ((b - a) ** 2 if a is not None and b is not None else 0.0))
    return g, sq


def read(g, sq, k, K, sg, left):
    if k - L13 < 0 or g[k] is None or g[k - L13] is None:
        return None
    rate = math.sqrt((sq[k] - sq[k - L13]) / L13)
    if rate <= 0:
        return None
    z = (g[k] - K) * sg / (rate * math.sqrt(max(left, 1.0)))
    s13 = (g[k] - g[k - L13]) * sg / (rate * math.sqrt(L13))
    return max(-9.0, min(9.0, z)), max(-9.0, min(9.0, s13))


for ser in CRYPTO:
    GRIDS[ser] = grid(variants.crypto_index(ser))
    g, sq = GRIDS[ser]
    for w in (x for x in wins if x["series"] == ser):
        close, K = float(w["close_ts"]), math.log(float(w["strike"]))
        opened = close - 900.0
        q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 20, close)).fetchall()
        if len(q) < 20:
            continue
        qt = [float(x[0]) for x in q]
        taken = set()
        for e in range(30, 871, 30):
            at = opened + e
            i = bisect.bisect_right(qt, at) - 1
            if i < 0 or at - qt[i] > 20:
                continue
            yb, ya = float(q[i][1]), float(q[i][2])
            k = int(at) - G0
            left = close - at
            for side, bid, ask in (("yes", yb, ya), ("no", 1 - ya, 1 - yb)):
                if side in taken or not (0.85 <= ask <= 0.97) or ask - bid > 0.04:
                    continue
                rd = read(g, sq, k, K, 1 if side == "yes" else -1, left)
                if rd is None:
                    continue
                taken.add(side)
                rows.append((close, left, ask, int(w["result"] == side), rd[0], rd[1], w["ticker"], side))
print("market sides at the first 30 s step with ask 0.85-0.97 (per step, so a side can appear once per start; here once per side):", len(rows))
mid = sorted(r[0] for r in rows)[len(rows) // 2]


def net(r):
    c = 1.0 / r[2]
    return ((1 - r[2]) * c if r[3] else -1.0) - fee(r[2]) * c


def line(label, rs):
    if not rs:
        print(f"   {label:34s} n=0")
        return
    n = len(rs)
    be = sum(r[2] + fee(r[2]) for r in rs) / n
    a = [net(r) for r in rs if r[0] <= mid]
    b = [net(r) for r in rs if r[0] > mid]
    print(f"   {label:34s} n={n:4d} won {100 * sum(r[3] for r in rs) / n:5.1f}% vs break-even {100 * be:5.1f}% | net per $1 {sum(net(r) for r in rs) / n:+.3f} | halves {sum(a) / max(len(a), 1):+.3f} (n={len(a)}) / {sum(b) / max(len(b), 1):+.3f} (n={len(b)}) | losers {sum(1 for r in rs if not r[3])}")


print("\nA. by minutes left at the first moment the side's ask is 0.85-0.97:")
for lo, hi in ((0, 2), (2, 4), (4, 6), (6, 9), (9, 12), (12, 15)):
    line(f"{lo}-{hi} min left", [r for r in rows if lo * 60 <= r[1] < hi * 60])
zone = [r for r in rows if 180 <= r[1] < 540]
print("\nB. 3-9 min left, by z (cushion over what the time left can move):")
for lo, hi in ((-9, 0), (0, 0.5), (0.5, 1), (1, 1.5), (1.5, 2.5), (2.5, 9.1)):
    line(f"z {lo} to {hi}", [r for r in zone if lo <= r[4] < hi])
print("\nC. 3-9 min left, by 13 minute slope of the cushion (typical moves; 0.5 = 26.6 degrees):")
for lo, hi in ((-9, -0.5), (-0.5, 0.5), (0.5, 1.5), (1.5, 9.1)):
    line(f"slope {lo} to {hi}", [r for r in zone if lo <= r[5] < hi])
print("\nB2. 3-9 min left, z under 1 by slope:")
for lo, hi in ((-9, -0.5), (-0.5, 0.5), (0.5, 9.1)):
    line(f"z<1, slope {lo} to {hi}", [r for r in zone if r[4] < 1 and lo <= r[5] < hi])

print("\nD. live bets at ask 0.80 or more since 11-02 (cushion z and 13 minute slope at the buy):")
bets = st.db.execute("SELECT ts,ticker,lane,side,ask,stake_usd,outcome FROM decisions WHERE action='bet' AND mode='trade' AND ts>? AND ask>=0.80 AND outcome IN ('yes','no') ORDER BY ts", (t0,)).fetchall()
byw = {w["ticker"].rsplit("-", 1)[0]: w for w in wins}
tot = lost = 0
rowsd = []
for b in bets:
    w = byw.get(b["ticker"].rsplit("-", 1)[0])
    if not w:
        continue
    g, sq = GRIDS[w["series"]]
    k = int(b["ts"]) - G0
    left = float(w["close_ts"]) - b["ts"]
    rd = read(g, sq, k, math.log(float(w["strike"])), 1 if b["side"] == "yes" else -1, left)
    if rd is None:
        continue
    won = b["outcome"] == b["side"]
    rowsd.append((b["lane"], b["ask"], left, rd[0], rd[1], won, b["stake_usd"]))
print("   bets read:", len(rowsd), "lost all:", sum(1 for r in rowsd if not r[5]), "stake lost $", round(sum(r[6] for r in rowsd if not r[5]), 2), "stake won on $", round(sum(r[6] for r in rowsd if r[5]), 2))
for lo, hi in ((-9, 1), (1, 2), (2, 3), (3, 9.1)):
    sub = [r for r in rowsd if lo <= r[3] < hi]
    print(f"   z {lo} to {hi}: n={len(sub)} lost all {sum(1 for r in sub if not r[5])} stake lost ${sum(r[6] for r in sub if not r[5]):.2f} stake on winners ${sum(r[6] for r in sub if r[5]):.2f}")
print("   the lost-all bets:")
for r in rowsd:
    if not r[5]:
        print(f"     {r[0]:20s} ask {r[1]:.2f} left {r[2]:5.0f}s z {r[3]:+.2f} slope13 {r[4]:+.2f} stake ${r[6]:.2f}")
print("DONE")
