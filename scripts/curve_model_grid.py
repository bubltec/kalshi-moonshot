"""A walk-forward grid on how the live curve model is fit (2026-11-08, user: "how are we sure those weights are best?" ... "i think the grid makes sense").

Question: the live model is a ridge regression of the net per $1 of buying a market side at its ask (held to the result, after the fee) on 18 readings (slope and variability of the spot's cushion at 1, 2, 3, 5, 8, 13, 21, 34 and 55 minutes) plus the ask, fit daily on the trailing 9 days with ridge 50 on every moment (curvemodel.fit). Does a different ridge strength, training window or row handling pick better bets on the NEXT day?
Moments: curvemodel.build_rows (the crypto_multiscale lane's rules: entry minutes, ask and spread bands, every 30 s, both sides, the 18 readings only, no time-left terms), every crypto series the lane trades with a settlement index, since the tape's index ticks begin. Walk-forward: for each test day, the model is fit on earlier days only (the first two days only train) and scores that day's moments; picks are the FIRST moment per market side whose predicted net per $1 is in the top Q share of the training predictions (Q = 5%, 10%, 20%, the cuts curvemodel stores) and above zero.
Grid: ridge strength 50 (live), 2,000, 50,000, 500,000 (on standardised readings, summed over rows: against 100,000 rows 50 is almost no shrinkage); training window all earlier days (the live 9), the last 4 days, the last 2 days; every moment, or every 10th moment (5 minutes apart: moments in one window overlap heavily). 24 settings x 3 shares. Baseline: every first moment in the same moments with no model ("all"). A setting is read against the live one with the standard errors given; with 72 cells some will lead by chance, so a winner must lead in both halves and by more than two standard errors of the difference to count. The grid itself was set before seeing results.
Standardisation (training mean and standard deviation of each reading) is taken over all days once, so day sums add up; it uses no outcome. Cuts are from the training predictions, as the live model does.
Not covered: the lane's other gates and exits, fills, the 60 s settlement average, time-left terms.

    scripts/cloud.sh run scripts/curve_model_grid.py
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


t_start = time.time()
S = {1: sums(1), 10: sums(10)}
print("day sums built", round(time.time() - t_start), "s", flush=True)
RIDGES = (50.0, 2000.0, 50000.0, 500000.0)
WINDOWS = (None, 4, 2)
EVERY = (1, 10)
QS = (0.05, 0.10, 0.20)
mid_close = sorted(m[0] for m in meta)[len(meta) // 2]


def pred(w, i):
    x = X[i]
    return sum(a * b for a, b in zip(w, x, strict=False))


results = {}  # (ridge, window, every) -> {q: [idx]}
for ridge in RIDGES:
    for win in WINDOWS:
        for every in EVERY:
            got = {q: [] for q in QS}
            for di, d in enumerate(days):
                if di < 2:
                    continue
                tr = days[max(0, di - win):di] if win else days[:di]
                A = [[0.0] * M for _ in range(M)]
                b = [0.0] * M
                for dd in tr:
                    Ad, bd, _ = S[every][dd]
                    for a in range(M):
                        b[a] += bd[a]
                        for c in range(M):
                            A[a][c] += Ad[a][c]
                for a in range(1, M):
                    A[a][a] += ridge
                w = solve(A, b)
                train_idx = [i for dd in tr for i in by_day[dd]][::5]
                ps = sorted(pred(w, i) for i in train_idx)
                cuts = {q: ps[int((1.0 - q) * (len(ps) - 1))] for q in QS}
                for i in by_day[d]:
                    p = pred(w, i)
                    for q in QS:
                        if p >= cuts[q] and p > 0:
                            got[q].append(i)
            results[(ridge, win, every)] = got
    print("ridge", ridge, "done", round(time.time() - t_start), "s", flush=True)

test_idx = [i for d in days[2:] for i in by_day[d]]


def first(idx):
    seen = {}
    for i in sorted(idx, key=lambda i: meta[i][1]):
        seen.setdefault((meta[i][2], meta[i][3]), i)
    return list(seen.values())


def stat(idx):
    if not idx:
        return None
    net = [Y[i] for i in idx]
    n = len(net)
    m = sum(net) / n
    se = (sum((x - m) ** 2 for x in net) / max(n - 1, 1)) ** 0.5 / n**0.5
    a = [Y[i] for i in idx if meta[i][0] <= mid_close]
    b = [Y[i] for i in idx if meta[i][0] > mid_close]
    return n, m, se, sum(a) / max(len(a), 1), len(a), sum(b) / max(len(b), 1), len(b)


def fmt(st):
    if st is None:
        return "n=0"
    n, m, se, ha, na, hb, nb = st
    return f"n={n:4d} net {m:+.3f} (se {se:.3f}) halves {ha:+.3f} ({na}) / {hb:+.3f} ({nb})"


print("\nbaseline: every first moment in the same moments, no model:", fmt(stat(first(test_idx))))
names = {None: "all earlier days", 4: "last 4 days", 2: "last 2 days"}
live = (50.0, None, 1)
print("\nthe live setting is ridge 50, all earlier days, every moment\n")
for q in QS:
    print(f"top {q:.0%} of training predictions, first per market side:")
    for key, got in results.items():
        ridge, win, every = key
        tag = "  <- live" if key == live else ""
        print(f"   ridge {ridge:>8.0f}  {names[win]:16s} {'every moment' if every == 1 else 'every 10th  '}  {fmt(stat(first(got[q])))}{tag}")
print("\nlead over the live setting (same picks counted per market side; a difference of means, with the standard error of the difference):")
for q in QS:
    base = first(results[live][q])
    bn = [Y[i] for i in base]
    bm = sum(bn) / max(len(bn), 1)
    bv = sum((x - bm) ** 2 for x in bn) / max(len(bn) - 1, 1)
    print(f"top {q:.0%}:")
    for key, got in results.items():
        if key == live:
            continue
        pk = first(got[q])
        if not pk:
            continue
        nn = [Y[i] for i in pk]
        nm = sum(nn) / len(nn)
        nv = sum((x - nm) ** 2 for x in nn) / max(len(nn) - 1, 1)
        se = (bv / max(len(bn), 1) + nv / len(nn)) ** 0.5
        ridge, win, every = key
        flag = "  *" if (nm - bm) > 2 * se else ""
        print(f"   ridge {ridge:>8.0f}  {names[win]:16s} {'every moment' if every == 1 else 'every 10th  '}  {nm - bm:+.3f} +- {se:.3f}{flag}")
print("DONE")
