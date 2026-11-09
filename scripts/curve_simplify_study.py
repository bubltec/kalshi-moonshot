"""Is the nine-scale Fibonacci model overkill, and does a curvature notation do as well? (2026-11-08, user: "is the Fibonacci overkill? ... maybe we should simplify it or use a different curvature notation").
Question: on the same moments and walk-forward as the other studies (every settled crypto 15-minute window since 11-01, both sides, every 30 s, the model lane's price band; each test day fitted on the earlier days only, every third training moment, the first three days train only), does the ridge on the 18 Fibonacci readings (slope and variability at 1, 2, 3, 5, 8, 13, 21, 34, 55 minutes) + the ask pick better than simpler sets?
Sets (grid fixed before the results): F18 the live model; F6 slope and variability at 3, 13 and 55 minutes + ask; F4 at 5 and 55 minutes + ask; ASK the ask alone (what the readings add); Q9 a curvature notation: over the last 5, 13 and 34 minutes the cushion path (25 samples, in that window's typical move) is fitted with a parabola a + b u + c u^2 (u from -1 to 0, 0 = now) and its level a, slope b and curvature c are the readings + ask; Q6 the same without the level (b and c); F6Q9 both sets.
Pick = the live rule: predicted net at or over the training top-share cut (5, 10, 20%) and above zero. Net per $1 after the fee, held to the result, first moment per market side: n, net with its standard error, win rate against the price, two halves each with its standard error.
Not covered: the lane's exits, queue, the 60-second settlement average.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/curve_simplify_study.py
"""

import collections
import math
import os
import time

from kalshi_moonshot import config, curvemodel

os.nice(15)
s = config.reload()
curvemodel.TRAIN_DAYS = (time.time() - 1_793_000_000) / 86400.0 + 0.5
_features18 = curvemodel.features
WINDOWS = (5, 13, 34)
NS = 25
U = [-1.0 + i / (NS - 1) for i in range(NS)]


def _pinv():
    # (X'X)^-1 X' for the design [1, u, u^2] on U, as three rows of NS weights
    xtx = [[0.0] * 3 for _ in range(3)]
    for u in U:
        v = (1.0, u, u * u)
        for i in range(3):
            for j in range(3):
                xtx[i][j] += v[i] * v[j]
    a = [row[:] + [1.0 if i == j else 0.0 for j in range(3)] for i, row in enumerate(xtx)]
    for c in range(3):
        p = max(range(c, 3), key=lambda r: abs(a[r][c]))
        a[c], a[p] = a[p], a[c]
        pv = a[c][c]
        a[c] = [x / pv for x in a[c]]
        for r in range(3):
            if r != c:
                f = a[r][c]
                a[r] = [x - f * y for x, y in zip(a[r], a[c], strict=False)]
    inv = [row[3:] for row in a]
    return [[sum(inv[i][j] * (1.0, u, u * u)[j] for j in range(3)) for u in U] for i in range(3)]


PINV = _pinv()


def features_all(g, sq, k, strike_log, sign, why=None, left=None):
    base = _features18(g, sq, k, strike_log, sign, why, left)
    if base is None:
        return None
    quad = []
    for m in WINDOWS:
        L = 60 * m
        rate = math.sqrt((sq[k] - sq[k - L]) / L)
        unit = rate * math.sqrt(L)
        ys = []
        for i in range(NS):
            x = g[k - L + round(i * L / (NS - 1))]
            if x is None:
                return None
            ys.append((x - strike_log) * sign / unit)
        quad += [sum(PINV[r][i] * ys[i] for i in range(NS)) for r in range(3)]  # a, b, c
    return base[:18] + quad


curvemodel.features = features_all
meta = []
rows = curvemodel.build_rows(s, meta=meta)
print("rows", len(rows), "market sides", len({(m[2], m[3]) for m in meta}))
FIB = curvemodel.FIB
pos = {m: j for j, m in enumerate(FIB)}


def fib_idx(scales):
    return [x for m in scales for x in (2 * pos[m], 2 * pos[m] + 1)]


Q_LEVEL = [18 + 3 * w for w in range(3)]
Q_SLOPE_CURV = [18 + 3 * w + d for w in range(3) for d in (1, 2)]
SETS = {
    "F18": list(range(18)),
    "F6": fib_idx((3, 13, 55)),
    "F4": fib_idx((5, 55)),
    "ASK": [],
    "Q9": list(range(18, 27)),
    "Q6": Q_SLOPE_CURV,
    "F6Q9": fib_idx((3, 13, 55)) + list(range(18, 27)),
}
day = [int(m[0] // 86400) for m in meta]
days = sorted(set(day))
by_day = collections.defaultdict(list)
for i, d in enumerate(day):
    by_day[d].append(i)
won = [1 if r[2] > 0 else 0 for r in rows]
mid = sorted(m[0] for m in meta)[len(meta) // 2]
picks = {n: {q: [] for q in (0.05, 0.10, 0.20)} for n in SETS}
for di, d in enumerate(days):
    if di < 3:
        continue
    train = [i for dd in days[:di] for i in by_day[dd]]
    test = by_day[d]
    sub = train[::3]
    for name, idx in SETS.items():
        def sel(i, idx=idx):
            return [rows[i][0][j] for j in idx]

        model = curvemodel.fit([(sel(i), rows[i][1], rows[i][2]) for i in sub])
        pred = {i: curvemodel.predict(model, sel(i), rows[i][1]) for i in test}
        for q in picks[name]:
            cut = model["cuts"][f"{q:g}"]
            picks[name][q] += [i for i in test if pred[i] >= cut and pred[i] > 0]
print("test days", len(days) - 3)


def first(idx):
    seen = {}
    for i in sorted(idx, key=lambda i: meta[i][1]):
        seen.setdefault((meta[i][2], meta[i][3]), i)
    return list(seen.values())


def ms(xs):
    n = len(xs)
    if n == 0:
        return "n=0"
    m = sum(xs) / n
    se = (sum((x - m) ** 2 for x in xs) / max(n - 1, 1)) ** 0.5 / math.sqrt(n)
    return f"{m:+.3f} +/- {se:.3f} (n={n})"


for q in (0.05, 0.10, 0.20):
    print(f"\ntop {q:.0%}, first per market side")
    for name in SETS:
        f = first(picks[name][q])
        nets = [rows[i][2] for i in f]
        a = [rows[i][2] for i in f if meta[i][0] <= mid]
        b = [rows[i][2] for i in f if meta[i][0] > mid]
        wr = 100 * sum(won[i] for i in f) / max(len(f), 1)
        print(f"   {name:6s} net per $1 {ms(nets)} won {wr:4.1f}% | first half {ms(a)} | second half {ms(b)}")
print("DONE")
