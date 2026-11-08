"""The win chance from the cushion and the time left, with curvature (2026-11-08, user: "a straight line is not going to help, we need something with curve and regression logic built in ... quadratic mathematics").

Question: on the same moments as scripts/curve_time_left_walkforward.py, does a model of the chance the side wins, built from the cushion over what the time left can move (a curved link) with quadratic terms, pick better than the 18-reading straight-line ridge? Edge per $1 = chance / ask - 1 - fee / ask.
Models, each fit on earlier days only and scored on the next, picks = predicted edge at or over the training top-share cut and above zero:
  ridge18   the live model (straight line on the 18 readings + ask).
  formula   no fit: the chance is the normal curve of the projected cushion at the 13 minute scale (win_13m).
  logit1    logistic regression on [projected cushion at 5 and 13 minutes, cushion z at 13, logit(ask)] (straight in those).
  quad      logistic regression with squares and cross products of [projected cushion 13m, cushion z 13m, sqrt(seconds left), logit(ask)] (14 terms): the curvature and the time-left interaction.
Reported as in the other script: every moment and first per market side, net per $1 after the fee, two halves, and by minutes into the window, at the best 5%, 10% and 20%. Training moments are every third (they overlap heavily).
Not covered: the 60-second settlement average, queue, sales before the result.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/curve_win_chance_walkforward.py
"""

import collections
import math
import os
import time

from kalshi_moonshot import config, curvemodel
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
s = config.reload()
curvemodel.TRAIN_DAYS = (time.time() - 1_793_000_000) / 86400.0 + 0.5
meta = []
rows = curvemodel.build_rows(s, meta=meta, timed=True)
print("rows", len(rows), "market sides", len({(m[2], m[3]) for m in meta}))
day = [int(m[0] // 86400) for m in meta]
days = sorted(set(day))
by_day = collections.defaultdict(list)
for i, d in enumerate(day):
    by_day[d].append(i)
won = [1 if r[2] > 0 else 0 for r in rows]
L_, Z2, Z5, Z13, Z34, P2, P5, P13, P34, W13 = 18, 19, 20, 21, 22, 23, 24, 25, 26, 29


def logit(p):
    p = min(max(p, 0.001), 0.999)
    return math.log(p / (1 - p))


def basic(i):
    r = rows[i][0]
    return [r[P5], r[P13], r[Z13], logit(rows[i][1])]


def quad_terms(i):
    r = rows[i][0]
    v = [r[P13], r[Z13], math.sqrt(max(r[L_], 0.0)), logit(rows[i][1])]
    return v + [v[a] * v[b] for a in range(4) for b in range(a, 4)]


def logfit(idx, fn, lam=5.0, iters=7):
    xs = [fn(i) for i in idx]
    n, k = len(xs), len(xs[0])
    mu = [sum(x[j] for x in xs) / n for j in range(k)]
    sd = [(sum((x[j] - mu[j]) ** 2 for x in xs) / n) ** 0.5 or 1.0 for j in range(k)]
    z = [[1.0] + [(x[j] - mu[j]) / sd[j] for j in range(k)] for x in xs]
    y = [won[i] for i in idx]
    m = k + 1
    w = [0.0] * m
    for _ in range(iters):
        g = [0.0] * m
        h = [[lam if a == b and a else 0.0 for b in range(m)] for a in range(m)]
        for x, yy in zip(z, y, strict=False):
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, sum(a * b for a, b in zip(w, x, strict=False))))))
            d, wt = yy - p, p * (1 - p)
            for a in range(m):
                g[a] += d * x[a]
                xa = x[a] * wt
                for b in range(a, m):
                    h[a][b] += xa * x[b]
        for a in range(m):
            for b in range(a):
                h[a][b] = h[b][a]
            if a:
                g[a] -= lam * w[a]
        aug = [h[a][:] + [g[a]] for a in range(m)]
        for c in range(m):
            piv = max(range(c, m), key=lambda r_: abs(aug[r_][c]))
            aug[c], aug[piv] = aug[piv], aug[c]
            pv = aug[c][c] or 1e-9
            aug[c] = [v / pv for v in aug[c]]
            for r_ in range(m):
                if r_ != c and aug[r_][c]:
                    f = aug[r_][c]
                    aug[r_] = [v - f * u for v, u in zip(aug[r_], aug[c], strict=False)]
        w = [w[a] + aug[a][m] for a in range(m)]

    def chance(i):
        x = fn(i)
        t = w[0] + sum(w[j + 1] * (x[j] - mu[j]) / sd[j] for j in range(k))
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, t))))

    return chance


def edge(p, i):
    a = rows[i][1]
    return p / a - 1.0 - fee(a) / a


def models(train, test):
    out = {}
    sub = train[::3]
    rid = curvemodel.fit([(rows[i][0][:18], rows[i][1], rows[i][2]) for i in sub])
    out["ridge18"] = (lambda i: curvemodel.predict(rid, rows[i][0][:18], rows[i][1]), train)
    out["formula"] = (lambda i: edge(rows[i][0][W13], i), train)
    c1 = logfit(sub, basic)
    out["logit1"] = (lambda i: edge(c1(i), i), train)
    c2 = logfit(sub, quad_terms)
    out["quad"] = (lambda i: edge(c2(i), i), train)
    return out


NAMES = ("ridge18", "formula", "logit1", "quad")
res = {v: {q: [] for q in (0.05, 0.1, 0.2)} for v in NAMES}
for di, d in enumerate(days):
    if di < 3:
        continue
    train = [i for dd in days[:di] for i in by_day[dd]]
    test = by_day[d]
    ms = models(train, test)
    for v, (fn, tr) in ms.items():
        pred_train = sorted(fn(i) for i in tr[::5])
        for q in res[v]:
            cut = pred_train[int((1.0 - q) * (len(pred_train) - 1))]
            res[v][q] += [i for i in test if fn(i) >= cut and fn(i) > 0]
print("test days", len(days) - 3)
mid = sorted(m[0] for m in meta)[len(meta) // 2]


def show(label, idx):
    if not idx:
        print(f"   {label:30s} n=0")
        return
    n = len(idx)
    a = [rows[i][2] for i in idx if meta[i][0] <= mid]
    b = [rows[i][2] for i in idx if meta[i][0] > mid]
    print(f"   {label:30s} n={n:5d} net per $1 {sum(rows[i][2] for i in idx) / n:+.3f} won {100 * sum(won[i] for i in idx) / n:4.1f}% | halves {sum(a) / max(len(a), 1):+.3f} (n={len(a)}) / {sum(b) / max(len(b), 1):+.3f} (n={len(b)})")


def first(idx):
    seen = {}
    for i in sorted(idx, key=lambda i: meta[i][1]):
        seen.setdefault((meta[i][2], meta[i][3]), i)
    return list(seen.values())


for q in (0.05, 0.1, 0.2):
    print(f"\ntop {q:.0%}")
    for v in NAMES:
        f = first(res[v][q])
        show(f"{v}, every moment", res[v][q])
        show(f"{v}, first per market side", f)
        for lo, hi in ((0, 240), (240, 480), (480, 900)):
            show(f"{v}, first, {lo // 60}-{hi // 60} min in", [i for i in f if lo <= meta[i][1] < hi])
print("DONE")
