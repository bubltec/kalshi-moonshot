"""The win chance from the cushion alone, monotone in it (2026-11-08, a review of curve_win_chance_walkforward.py: the quad terms could not make a monotone curve, were near-collinear and nearly unregularised, sqrt(left) re-introduced what z removes, the `formula` baseline carried the trailing drift forward so the plain curve was never tested, and the cut was computed in-sample).
Question: on the same moments and walk-forward as curve_time_left_walkforward.py, does a win chance that rises steadily with the cushion beat the straight-line ridge and the price? Edge per $1 = chance / ask - 1 - fee / ask, buy where it is at or over a fixed edge bar (0.02, 0.05, 0.10: the bar is in dollars of expected net, so no in-sample quantile is involved) and above zero; the ridge's own prediction is a net per $1, so it takes the same bars, and also its top-5/10/20% as the live bot reads it.
Models, each fit on earlier days only (every third moment), scored on the next day:
  ridge18  the live model.
  phi_z    no fit: the chance is the normal curve of the cushion over what the seconds left can move (z at 13 minutes, no drift) = Phi(Z13). A martingale spot.
  phi_z34  the same at the 34 minute rate.
  iso      Phi-free: the win rate by z bucket (60 equal-count buckets of z at 13 minutes on the training days), made non-decreasing by pooling adjacent buckets (isotonic), looked up for the moment's z. Ignores the ask.
  probit   a logistic regression, monotone in every cushion input (z at 5, 13, 34 minutes, the drift term of the 13 and 34 minute projections = projection - z, logit(ask)), ridge 0.01 per training row (set before seeing results), no squares.
  probit_z the same without the drift terms (z at 5, 13, 34 and logit(ask)).
Also: calibration of probit's edge by decile on the test days, and the fade (buy the other side where Phi(Z13) or Phi(Z34) beats this side's ask by D, D in 0.05, 0.10, 0.15, 0.20).
Reported: every moment and first per market side, net per $1 after the full fee, win rate, two halves, by minutes into the window.
Not covered: the 60-second settlement average, queue, sales before the result.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/curve_win_chance_v2_walkforward.py
"""

import bisect
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
Z5, Z13, Z34, P13, P34 = 20, 21, 22, 25, 26
SQ2 = math.sqrt(2.0)


def phi(x):
    return 0.5 * (1.0 + math.erf(x / SQ2))


def logit(p):
    p = min(max(p, 0.001), 0.999)
    return math.log(p / (1 - p))


def edge(p, i):
    a = rows[i][1]
    return p / a - 1.0 - fee(a) / a


def probit_full(i):
    r = rows[i][0]
    return [r[Z5], r[Z13], r[Z34], r[P13] - r[Z13], r[P34] - r[Z34], logit(rows[i][1])]


def probit_z(i):
    r = rows[i][0]
    return [r[Z5], r[Z13], r[Z34], logit(rows[i][1])]


def logfit(idx, fn, iters=7):
    xs = [fn(i) for i in idx]
    n, k = len(xs), len(xs[0])
    lam = 0.01 * n
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


def isotonic(idx, nb=60):
    pts = sorted((rows[i][0][Z13], won[i]) for i in idx)
    size = max(len(pts) // nb, 1)
    blocks = []  # [sum of wins, count, upper z edge]
    for a in range(0, len(pts), size):
        ch = pts[a : a + size]
        blocks.append([sum(w for _, w in ch), len(ch), ch[-1][0]])
    out = []
    for b in blocks:
        out.append(b[:])
        while len(out) > 1 and out[-2][0] / out[-2][1] > out[-1][0] / out[-1][1]:
            top = out.pop()
            out[-1][0] += top[0]
            out[-1][1] += top[1]
            out[-1][2] = top[2]
    edges = [b[2] for b in out]
    rates = [b[0] / b[1] for b in out]

    def chance(i):
        return rates[min(bisect.bisect_left(edges, rows[i][0][Z13]), len(rates) - 1)]

    return chance


def models(train):
    sub = train[::3]
    rid = curvemodel.fit([(rows[i][0][:18], rows[i][1], rows[i][2]) for i in sub])
    c_iso = isotonic(sub)
    c_pf = logfit(sub, probit_full)
    c_pz = logfit(sub, probit_z)
    return {
        "ridge18": lambda i: curvemodel.predict(rid, rows[i][0][:18], rows[i][1]),
        "phi_z": lambda i: edge(phi(rows[i][0][Z13]), i),
        "phi_z34": lambda i: edge(phi(rows[i][0][Z34]), i),
        "iso": lambda i: edge(c_iso(i), i),
        "probit": lambda i: edge(c_pf(i), i),
        "probit_z": lambda i: edge(c_pz(i), i),
    }


NAMES = ("ridge18", "phi_z", "phi_z34", "iso", "probit", "probit_z")
BARS = (0.02, 0.05, 0.10)
picks = {v: {b: [] for b in BARS} for v in NAMES}
ridge_top = {q: [] for q in (0.05, 0.1, 0.2)}
calib = collections.defaultdict(list)
for di, d in enumerate(days):
    if di < 3:
        continue
    train = [i for dd in days[:di] for i in by_day[dd]]
    test = by_day[d]
    ms = models(train)
    for v, fn in ms.items():
        pr = {i: fn(i) for i in test}
        for b in BARS:
            picks[v][b] += [i for i in test if pr[i] >= b]
        if v == "probit":
            for i in test:
                calib[min(9, max(0, int((pr[i] + 0.3) / 0.06)))].append((pr[i], rows[i][2]))
        if v == "ridge18":
            tr = sorted(fn(i) for i in train[::5])
            for q in ridge_top:
                cut = tr[int((1.0 - q) * (len(tr) - 1))]
                ridge_top[q] += [i for i in test if pr[i] >= cut and pr[i] > 0]
print("test days", len(days) - 3)
mid = sorted(m[0] for m in meta)[len(meta) // 2]


def show(label, idx):
    if not idx:
        print(f"   {label:34s} n=0")
        return
    n = len(idx)
    a = [rows[i][2] for i in idx if meta[i][0] <= mid]
    b = [rows[i][2] for i in idx if meta[i][0] > mid]
    print(f"   {label:34s} n={n:5d} net per $1 {sum(rows[i][2] for i in idx) / n:+.3f} won {100 * sum(won[i] for i in idx) / n:4.1f}% | halves {sum(a) / max(len(a), 1):+.3f} (n={len(a)}) / {sum(b) / max(len(b), 1):+.3f} (n={len(b)})")


def first(idx):
    seen = {}
    for i in sorted(idx, key=lambda i: meta[i][1]):
        seen.setdefault((meta[i][2], meta[i][3]), i)
    return list(seen.values())


def block(label, idx):
    f = first(idx)
    show(f"{label}, every moment", idx)
    show(f"{label}, first per market side", f)
    for lo, hi in ((0, 240), (240, 480), (480, 900)):
        show(f"{label}, first, {lo // 60}-{hi // 60} min in", [i for i in f if lo <= meta[i][1] < hi])


for b in BARS:
    print(f"\npredicted edge at or over {b:.2f} per $1")
    for v in NAMES:
        block(v, picks[v][b])
for q in ridge_top:
    print(f"\nridge18 top {q:.0%} as the live bot reads it")
    block("ridge18", ridge_top[q])
print("\ncalibration of probit's predicted edge on the test days (bucket: n, mean predicted, mean realized net per $1):")
for k in sorted(calib):
    v = calib[k]
    print(f"   bucket {k}: n={len(v)} predicted {sum(x[0] for x in v) / len(v):+.3f} realized {sum(x[1] for x in v) / len(v):+.3f}")


def other(i):
    ask = 1.0 - meta[i][4]
    w = rows[i][2] <= 0
    c = 1.0 / ask
    return w, ask, ((1 - ask) * c if w else -1.0) - fee(ask) * c


print("\nfade: buy the other side where the plain normal curve beats this side's ask by D")
for zc, nm in ((Z13, "13"), (Z34, "34")):
    for dd in (0.05, 0.10, 0.15, 0.20):
        pick = first([i for i in range(len(rows)) if phi(rows[i][0][zc]) - rows[i][1] >= dd])
        if not pick:
            print(f"   scale {nm} D {dd:.2f} n=0")
            continue
        sc = [other(i) for i in pick]
        n = len(pick)
        ha = [x[2] for x, i in zip(sc, pick, strict=True) if meta[i][0] <= mid]
        hb = [x[2] for x, i in zip(sc, pick, strict=True) if meta[i][0] > mid]
        be = sum(a + fee(a) for _, a, _ in sc) / n
        print(f"   scale {nm} D {dd:.2f} first per market side n={n:5d} won {100 * sum(1 for x in sc if x[0]) / n:4.1f}% vs break-even {100 * be:4.1f}% | net per $1 {sum(x[2] for x in sc) / n:+.3f} | halves {sum(ha) / max(len(ha), 1):+.3f} / {sum(hb) / max(len(hb), 1):+.3f}")
print("DONE")
