"""Does the curve model's edge depend on the time left in the window? (2026-11-08, user: "the time frame of 15 mins is super important to the logic and I don't think we have put enough importance on that")

Question: in the walk-forward of scripts/curve_model_grid.py (the live setting: ridge 50, all earlier days, every moment; the first two days only train), what do the model's picks and the no-model baseline return per $1, by minutes into the 15-minute window (two-minute buckets)? Every moment (not only the first per market side) so each bucket has its own count; picks are the top 10% and 20% of training predictions and above zero. Net per $1 after the fee, held to the result, with standard error, and the two halves of the period.
Not covered: the lane's other gates and exits, fills.

    scripts/cloud.sh run scripts/curve_model_by_time.py
"""

import collections
import math
import os
import time

from kalshi_moonshot import config, curvemodel

os.nice(15)
s = config.reload()
curvemodel.TRAIN_DAYS = (time.time() - 1_793_000_000) / 86400.0 + 0.5
meta = []
rows = curvemodel.build_rows(s, meta=meta, timed=False)
print("rows", len(rows), "market sides", len({(m[2], m[3]) for m in meta}), flush=True)
K = len(rows[0][0]) + 1  # 18 readings + the ask
n_all = len(rows)
mu = [0.0] * K
for r in rows:
    for j, v in enumerate([*r[0], r[1]]):
        mu[j] += v
mu = [m / n_all for m in mu]
sd = [0.0] * K
for r in rows:
    for j, v in enumerate([*r[0], r[1]]):
        sd[j] += (v - mu[j]) ** 2
sd = [(x / n_all) ** 0.5 or 1.0 for x in sd]
X = [[1.0] + [(v - mu[j]) / sd[j] for j, v in enumerate([*r[0], r[1]])] for r in rows]
Y = [r[2] for r in rows]
day = [int(m[0] // 86400) for m in meta]
days = sorted(set(day))
print("UTC days", days, flush=True)
by_day = collections.defaultdict(list)
for i, d in enumerate(day):
    by_day[d].append(i)
M = K + 1


def sums(every):
    out = {}
    for d in days:
        A = [[0.0] * M for _ in range(M)]
        b = [0.0] * M
        for pos, i in enumerate(by_day[d]):
            if pos % every:
                continue
            x, y = X[i], Y[i]
            for a in range(M):
                xa = x[a]
                b[a] += xa * y
                Aa = A[a]
                for c in range(a, M):
                    Aa[c] += xa * x[c]
        for a in range(M):
            for c in range(a):
                A[a][c] = A[c][a]
        out[d] = (A, b, sum(1 for pos in range(len(by_day[d])) if pos % every == 0))
    return out


def solve(A, b):
    n = len(b)
    aug = [A[i][:] + [b[i]] for i in range(n)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r_: abs(aug[r_][c]))
        aug[c], aug[piv] = aug[piv], aug[c]
        pv = aug[c][c] or 1e-12
        aug[c] = [v / pv for v in aug[c]]
        for r_ in range(n):
            if r_ != c and aug[r_][c]:
                f = aug[r_][c]
                aug[r_] = [v - f * u for v, u in zip(aug[r_], aug[c], strict=False)]
    return [aug[i][n] for i in range(n)]



S = sums_all = None
S = {}
for d in days:
    A = [[0.0] * M for _ in range(M)]
    b = [0.0] * M
    for i in by_day[d]:
        x, y = X[i], Y[i]
        for a in range(M):
            xa = x[a]
            b[a] += xa * y
            Aa = A[a]
            for c in range(a, M):
                Aa[c] += xa * x[c]
    for a in range(M):
        for c in range(a):
            A[a][c] = A[c][a]
    S[d] = (A, b)
mid_close = sorted(m[0] for m in meta)[len(meta) // 2]
picks = {0.10: [], 0.20: []}
for di, d in enumerate(days):
    if di < 2:
        continue
    A = [[0.0] * M for _ in range(M)]
    b = [0.0] * M
    for dd in days[:di]:
        Ad, bd = S[dd]
        for a in range(M):
            b[a] += bd[a]
            for c in range(M):
                A[a][c] += Ad[a][c]
    for a in range(1, M):
        A[a][a] += 50.0
    w = solve(A, b)
    tr = [i for dd in days[:di] for i in by_day[dd]][::5]
    ps = sorted(sum(p * q for p, q in zip(w, X[i], strict=False)) for i in tr)
    cuts = {q: ps[int((1.0 - q) * (len(ps) - 1))] for q in picks}
    for i in by_day[d]:
        p = sum(a * b_ for a, b_ in zip(w, X[i], strict=False))
        for q in picks:
            if p >= cuts[q] and p > 0:
                picks[q].append(i)
test_idx = [i for d in days[2:] for i in by_day[d]]


def stat(idx):
    if len(idx) < 2:
        return "n=%d" % len(idx)
    net = [Y[i] for i in idx]
    n = len(net)
    m = sum(net) / n
    se = (sum((x - m) ** 2 for x in net) / (n - 1)) ** 0.5 / n**0.5
    a = [Y[i] for i in idx if meta[i][0] <= mid_close]
    c = [Y[i] for i in idx if meta[i][0] > mid_close]
    return f"n={n:5d} net {m:+.3f} (se {se:.3f}) halves {sum(a) / max(len(a), 1):+.3f} ({len(a)}) / {sum(c) / max(len(c), 1):+.3f} ({len(c)})"


print("every moment, by minutes into the window (two-minute buckets), test days only:")
for lo in range(2, 14, 2):
    sel = lambda ids: [i for i in ids if lo * 60 <= meta[i][1] < (lo + 2) * 60]
    print(f"  {lo:2d}-{lo + 2:2d} min in ({15 - lo - 2}-{15 - lo} min left)")
    print(f"     no model     {stat(sel(test_idx))}")
    print(f"     top 20%      {stat(sel(picks[0.20]))}")
    print(f"     top 10%      {stat(sel(picks[0.10]))}")
print("DONE")
