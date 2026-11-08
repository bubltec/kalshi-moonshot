"""The accumulator: add to a bet that is winning, late (2026-11-07, user: "once it passes 85c and the hour and 15m (55, 13 in Fibonacci) at under 120s left we should tack on another 2-5x the bet so we make more").

Question: on every settled crypto 15-minute window, a market side whose ask has passed 0.85 with 120 s or less left and whose cushion (the spot's distance from the strike on that side) has risen over both the 55 and the 13 minute look-backs: does buying it then pay after the fee, at what win rate against the price, and what does the loss tail look like at 2x to 5x the first stake?
The reading is the lane's own: slope = change of the cushion over the look-back in that look-back's typical move (tan of the angle; at least 0.5 is rising), each look-back's typical move from the tape's one-second log moves. The add-on is the first moment inside the last 120 s (one-second quote steps) that clears the bar, one per market side. Bars compared: the cushion also over 1.0 typical minutes of the remaining time (z) and the slope bars 0 and 0.5. Two halves of the period, ask bands, and the cushion read by z. The break-even win rate at each price is printed beside the actual win rate.
Not covered: the first bet (the add-on is scored alone, as if bought regardless of an earlier buy), queue and depth: a $5-15 add-on at 0.90+ is small against the book but the ask is taken at its displayed size; the fee is paid in full on every add-on (no resting bid).

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/accumulator_study.py
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

os.nice(15)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
t0 = calendar.timegm((2026, 11, 2, 6, 0, 0))
CRYPTO = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
wins = st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND series IN (?,?,?,?,?) AND result IN ('yes','no') AND strike>0 ORDER BY series,close_ts", (t0, *CRYPTO)).fetchall()
G0 = int(t0 - 4300)
G1 = int(max(float(w["close_ts"]) for w in wins)) + 2
rows = []  # (close, ask, won, z, slope13, slope55, ticker, side)


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


def slope(g, sq, k, K, sg, L):
    if k - L < 0 or g[k] is None or g[k - L] is None:
        return None
    rate = math.sqrt((sq[k] - sq[k - L]) / L)
    if rate <= 0:
        return None
    return ((g[k] - K) * sg - (g[k - L] - K) * sg) / (rate * math.sqrt(L)), rate


for ser in CRYPTO:
    g, sq = grid(variants.crypto_index(ser))
    for w in (x for x in wins if x["series"] == ser):
        close, K = float(w["close_ts"]), math.log(float(w["strike"]))
        q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], close - 900, close)).fetchall()
        if len(q) < 20:
            continue
        qt = [float(x[0]) for x in q]
        for side in ("yes", "no"):
            sg = 1 if side == "yes" else -1
            taken = False
            for sec in range(120, 5, -1):  # seconds left, first moment that clears the price bar
                at = close - sec
                i = bisect.bisect_right(qt, at) - 1
                if i < 0 or at - qt[i] > 20:
                    continue
                yb, ya = float(q[i][1]), float(q[i][2])
                ask = ya if side == "yes" else 1 - yb
                if ask < 0.85 or ask > 0.99:
                    continue
                k = int(at) - G0
                a13 = slope(g, sq, k, K, sg, 13 * 60)
                a55 = slope(g, sq, k, K, sg, 55 * 60)
                if a13 is None or a55 is None or g[k] is None:
                    continue
                rate1s = a13[1]
                z = (g[k] - K) * sg / (rate1s * math.sqrt(max(sec, 1)))
                rows.append((close, ask, int(w["result"] == side), z, a13[0], a55[0], w["ticker"], side))
                taken = True
                break
            if taken:
                continue
mid = sorted(r[0] for r in rows)[len(rows) // 2] if rows else 0
print("add-on candidates (first moment per market side with ask 0.85-0.99 inside the last 120 s, both look-backs readable):", len(rows), "market sides", len({(r[6], r[7]) for r in rows}))


def net(r):
    c = 1.0 / r[1]
    return ((1 - r[1]) * c if r[2] else -1.0) - fee(r[1]) * c


def line(label, rs):
    if not rs:
        print(f"   {label:44s} n=0")
        return
    n = len(rs)
    wr = sum(r[2] for r in rs) / n
    ask = sum(r[1] for r in rs) / n
    be = sum(r[1] + fee(r[1]) for r in rs) / n  # win rate that breaks even after the fee
    a = [net(r) for r in rs if r[0] <= mid]
    b = [net(r) for r in rs if r[0] > mid]
    tot = sum(net(r) for r in rs)
    print(f"   {label:44s} n={n:4d} won {100 * wr:5.1f}% vs break-even {100 * be:5.1f}% | net per $1 {tot / n:+.3f} | halves {sum(a) / max(len(a), 1):+.3f} (n={len(a)}) / {sum(b) / max(len(b), 1):+.3f} (n={len(b)}) | losers {sum(1 for r in rs if not r[2])}")


print("\nthe bars (slope in typical moves of the look-back; 0.5 = 26.6 degrees):")
line("price bar only (ask 0.85-0.99, last 120 s)", rows)
for s13, s55 in ((0.0, 0.0), (0.5, 0.5), (0.5, 0.0), (1.0, 1.0)):
    line(f"cushion rising 13m >= {s13:.1f} and 55m >= {s55:.1f}", [r for r in rows if r[4] >= s13 and r[5] >= s55])
line("the user's rule (13m and 55m >= 0.5)", [r for r in rows if r[4] >= 0.5 and r[5] >= 0.5])
line("against: 13m or 55m falling (<= -0.5)", [r for r in rows if r[4] <= -0.5 or r[5] <= -0.5])
print("\nby ask band (the user's rule):")
us = [r for r in rows if r[4] >= 0.5 and r[5] >= 0.5]
for a, b in ((0.85, 0.90), (0.90, 0.95), (0.95, 1.0)):
    line(f"ask {a:.2f} to {b:.2f}", [r for r in us if a <= r[1] < b])
print("\nby cushion z (the user's rule; z = cushion over what the remaining seconds can move):")
for a, b in ((-9, 1), (1, 2), (2, 3), (3, 99)):
    line(f"z {a} to {b}", [r for r in us if a <= r[3] < b])
print("\nthe tail: the first stake S plus an add-on of k x S at the average price of the rule's picks; the add-on's net in units of S")
if us:
    avg_ask = sum(r[1] for r in us) / len(us)
    wr = sum(r[2] for r in us) / len(us)
    for k in (2, 3, 4, 5):
        tot = sum(net(r) for r in us) * k
        worst = k * 1.0
        print(f"   {k}x: add-on total {tot:+.2f} S over {len(us)} picks, one lost add-on costs {worst:.0f} S + fee, a win pays {k * (1 - avg_ask):.2f} S; the first win rate {wr:.3f} at {avg_ask:.3f}")
print("DONE")
