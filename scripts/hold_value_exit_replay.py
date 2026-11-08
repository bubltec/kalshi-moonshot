"""Exit while still ahead, read from the curve (2026-11-07, user: "detect a good time to drop out while still profitable, not exactly scalping").

Question: at any moment of a held side, the sell value is the bid less the taker fee and the hold value is the chance the side wins times $1. If the curve's own estimate of that chance (the cushion's z for the seconds left and its 13 and 55 minute slopes, plus the bid) is under the sell value by a margin, does selling at that moment beat holding to the result, net of the fee?
Setup, on every settled crypto 15-minute window since 11-02, both sides, every 30 s from minute 2 to 14.7 (20 s or more left, a sale cannot go in the last seconds), bid 0.30-0.97 with a spread of 0.04 or less: a logistic estimate of P(win) on [1, z, slope 13m, slope 55m, logit(bid)], fit walk-forward (each test day on the earlier days only; the first three days only train). The policy sells at the first moment of a market side where bid - fee - P(win) is over the margin, and is compared with holding the same position to the result: change = sale proceeds - what the position pays (1 if it won, 0 if not), per contract and in dollars at the bid paid for it (cost 1 contract = the bid shown at the first moment the position is held, which is not known here, so the table is per contract and per $1 of sale value). Margins 0, 0.02, 0.05, 0.10 are a grid, not a choice after the fact. Two halves of the test days, bid bands, and curve shapes (the direction of the 13 minute cushion and the 55 minute one).
Not covered: the entry (a position is assumed held at any moment: the bid band shows where the sale matters), the lane's own exits (take-profit, trail, shock, cash-out) that sell on other rules, and the sell fee on a resting sell (zero). The sale is assumed filled at the bid shown.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/hold_value_exit_replay.py
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
LOOK13, LOOK55 = 13 * 60, 55 * 60
rows = []  # (close, ticker, side, e, bid, won, x[5], s13, s55)


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


for ser in CRYPTO:
    g, sq = grid(variants.crypto_index(ser))
    for w in (x for x in wins if x["series"] == ser):
        close, K = float(w["close_ts"]), math.log(float(w["strike"]))
        opened = close - 900.0
        q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 20, close)).fetchall()
        if len(q) < 20:
            continue
        qt = [float(x[0]) for x in q]
        for e in range(120, 881, 30):
            at = opened + e
            i = bisect.bisect_right(qt, at) - 1
            if i < 0 or at - qt[i] > 20:
                continue
            yb, ya = float(q[i][1]), float(q[i][2])
            k = int(at) - G0
            left = close - at
            if k - LOOK55 < 0 or g[k] is None or g[k - LOOK13] is None or g[k - LOOK55] is None:
                continue
            r13 = math.sqrt((sq[k] - sq[k - LOOK13]) / LOOK13)
            r55 = math.sqrt((sq[k] - sq[k - LOOK55]) / LOOK55)
            if r13 <= 0 or r55 <= 0:
                continue
            for side, bid, ask in (("yes", yb, ya), ("no", 1 - ya, 1 - yb)):
                if not (0.30 <= bid <= 0.97) or ask - bid > 0.04:
                    continue
                sg = 1 if side == "yes" else -1
                z = (g[k] - K) * sg / (r13 * math.sqrt(left))
                s13 = (g[k] - g[k - LOOK13]) * sg / (r13 * math.sqrt(LOOK13))
                s55 = (g[k] - g[k - LOOK55]) * sg / (r55 * math.sqrt(LOOK55))
                z, s13, s55 = max(-6.0, min(6.0, z)), max(-6.0, min(6.0, s13)), max(-6.0, min(6.0, s55))
                lb = math.log(bid / (1 - bid))
                rows.append((close, w["ticker"], side, e, bid, int(w["result"] == side), [1.0, z, s13, s55, lb], s13, s55))
print("moments", len(rows), "market sides", len({(r[1], r[2]) for r in rows}))
day = lambda r: int(r[0] // 86400)
days = sorted({day(r) for r in rows})
by_day = collections.defaultdict(list)
for r in rows:
    by_day[day(r)].append(r)


def fit(rs, iters=8, lam=1.0):
    n = len(rs[0][6])
    w = [0.0] * n
    for _ in range(iters):
        grad = [0.0] * n
        hess = [[lam if i == j else 0.0 for j in range(n)] for i in range(n)]
        for r in rs:
            x = r[6]
            p = 1.0 / (1.0 + math.exp(-sum(a * b for a, b in zip(w, x, strict=False))))
            d = r[5] - p
            wt = p * (1 - p)
            for i in range(n):
                grad[i] += d * x[i]
                for j in range(n):
                    hess[i][j] += wt * x[i] * x[j]
        for i in range(n):
            grad[i] -= lam * w[i]
        # solve hess * step = grad (Gauss-Jordan)
        a = [row[:] + [grad[i]] for i, row in enumerate(hess)]
        for c in range(n):
            piv = max(range(c, n), key=lambda r_: abs(a[r_][c]))
            a[c], a[piv] = a[piv], a[c]
            for r_ in range(n):
                if r_ != c:
                    f = a[r_][c] / a[c][c]
                    for cc in range(c, n + 1):
                        a[r_][cc] -= f * a[c][cc]
        w = [w[i] + a[i][n] / a[i][i] for i in range(n)]
    return w


test = []  # (row, P(win))
for di, d in enumerate(days):
    if di < 3:
        continue
    train = [r for dd in days[:di] for r in by_day[dd]]
    w = fit(train)
    for r in by_day[d]:
        test.append((r, 1.0 / (1.0 + math.exp(-sum(a * b for a, b in zip(w, r[6], strict=False))))))
print("test moments", len(test), "test days", len(days) - 3)
mid = sorted(r[0][0] for r in test)[len(test) // 2]
calib = collections.defaultdict(lambda: [0, 0.0, 0])
for r, p in test:
    c = calib[min(9, int(p * 10))]
    c[0] += 1
    c[1] += p
    c[2] += r[5]
print("calibration of the estimate on the test days (decile: n, mean estimate, actual win rate):", {k: (v[0], round(v[1] / v[0], 3), round(v[2] / v[0], 3)) for k, v in sorted(calib.items())})


def policy(margin, bandfn=None, shapefn=None):
    first = {}
    for r, p in sorted(test, key=lambda x: x[0][3]):
        key = (r[1], r[2])
        if key in first:
            continue
        if bandfn and not bandfn(r):
            continue
        if shapefn and not shapefn(r):
            continue
        sell = r[4] - fee(r[4])
        if sell - p > margin:
            first[key] = (r, p, sell - r[5])
    return list(first.values())


def line(label, got):
    if not got:
        print(f"   {label:40s} n=0")
        return
    n = len(got)
    ch = [x[2] for x in got]
    a = [x[2] for x in got if x[0][0] <= mid]
    b = [x[2] for x in got if x[0][0] > mid]
    beat = sum(1 for c in ch if c > 0)
    print(f"   {label:40s} sells {n:5d} | change vs hold per contract {sum(ch) / n:+.3f} total {sum(ch):+.1f} | beat hold {beat} lost {n - beat} | halves {sum(a) / max(len(a), 1):+.3f} (n={len(a)}) / {sum(b) / max(len(b), 1):+.3f} (n={len(b)})")


print("\nthe policy by margin (sale proceeds minus the hold payoff, first moment per market side where bid - fee - P(win) is over the margin):")
for m in (0.0, 0.02, 0.05, 0.10):
    line(f"margin {m:.2f}", policy(m))
for M in (0.02, 0.05):
    print(f"\nby bid band (margin {M:.2f}):")
    for lo, hi in ((0.30, 0.50), (0.50, 0.70), (0.70, 0.85), (0.85, 0.98)):
        line(f"bid {lo:.2f}-{hi:.2f}", policy(M, bandfn=lambda r, lo=lo, hi=hi: lo <= r[4] < hi))
    print(f"\nby curve shape (margin {M:.2f}; 13m / 55m cushion direction at the sale):")
    dr = lambda v: "up" if v >= 0.5 else "down" if v <= -0.5 else "flat"
    for a in ("up", "flat", "down"):
        for b in ("up", "flat", "down"):
            line(f"13m {a} / 55m {b}", policy(M, shapefn=lambda r, a=a, b=b: dr(r[7]) == a and dr(r[8]) == b))
print("DONE")
