"""Checks on the win-chance study after a second review (2026-11-08): what the probit's coefficients say, whether the volatility estimate is the reason Phi(z) loses, a link that tests "does it beat the plain curve", and standard errors on everything.
A. Variance ratio, per coin and span: the variance of the log price change over `span` seconds (non-overlapping) divided by span x the one-second variance, on the last 3 days of index ticks. 1.0 = a random walk at the one-second rate; above 1 the real spread over minutes is wider than the one-second rate scales to (trend, jumps), below 1 it mean-reverts.
B. The probit's coefficients (on the standardised inputs) fitted on all the days: z at 5, 13 and 34 minutes, the drift terms (projection - z) at 13 and 34 minutes, logit(ask). The sign of a drift coefficient says whether trailing movement in the bet's favour raises (momentum) or lowers (reversion) the win chance once z is in.
C. One volatility scale on z: P = Phi(z / k), k fitted by maximum likelihood on the training days (grid 0.7 to 2.5), walk-forward; the k per test day is printed. And a logistic on [logit(Phi(Z13)), logit(ask)] and with the drift terms added, ridge 0.01 per row: the plain curve enters as a feature with a free coefficient, so "does the fit add to the plain curve" is a direct test.
D. The buys of the model picks at fixed edge bars (0.02, 0.05, 0.10 per $1 expected net), first per market side, with a standard error (sd / sqrt n) and the halves, and the tails of the plain curve: buying the side itself where Phi(Z13) beats its ask by D, in bands 0.05-0.10, 0.10-0.15, 0.15-0.20, 0.20-0.30, 0.30 and over.
Walk-forward as in the other studies (each test day on the earlier days only, every third training moment, the first three days train only).

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/curve_win_chance_v3_checks.py
"""

import collections
import itertools
import math
import os
import time

from kalshi_moonshot import config, curvemodel, db, variants
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
s = config.reload()
SQ2 = math.sqrt(2.0)
phi = lambda x: 0.5 * (1.0 + math.erf(x / SQ2))  # noqa: E731


def logit(p):
    p = min(max(p, 0.001), 0.999)
    return math.log(p / (1 - p))


# A. variance ratio
tape = db.connect(s.tape_path, "tape", shared=True)
now = time.time()
print("A. variance ratio (realised variance over span / (span x one-second variance)), last 3 days of ticks")
SPANS = (15, 60, 300, 900)
print("   coin        " + " ".join(f"{sp:>6d}s" for sp in SPANS))
for ser in ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXNEAR15M", "KXBNB15M", "KXHYPE15M", "KXZEC15M"):
    ix = variants.crypto_index(ser)
    r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, now - 3 * 86400)).fetchall()
    if len(r) < 5000:
        print(f"   {ser[2:6]:10s} too few ticks")
        continue
    ts = [float(x[0]) for x in r]
    vs = [float(x[1]) for x in r]
    t0, t1 = int(ts[0]), int(ts[-1])
    g, j = [], 0
    last = vs[0]
    for t in range(t0, t1):
        while j < len(ts) and ts[j] <= t:
            last = vs[j]
            j += 1
        g.append(math.log(last))
    v1 = sum((b - a) ** 2 for a, b in itertools.pairwise(g)) / (len(g) - 1)
    out = []
    for sp in SPANS:
        d = [g[i + sp] - g[i] for i in range(0, len(g) - sp, sp)]
        out.append(sum(x * x for x in d) / len(d) / (sp * v1) if v1 > 0 else float("nan"))
    print(f"   {ser[2:6]:10s} " + " ".join(f"{x:7.2f}" for x in out))

# rows
curvemodel.TRAIN_DAYS = (time.time() - 1_793_000_000) / 86400.0 + 0.5
meta = []
rows = curvemodel.build_rows(s, meta=meta, timed=True)
print("\nrows", len(rows), "market sides", len({(m[2], m[3]) for m in meta}))
day = [int(m[0] // 86400) for m in meta]
days = sorted(set(day))
by_day = collections.defaultdict(list)
for i, d in enumerate(day):
    by_day[d].append(i)
won = [1 if r[2] > 0 else 0 for r in rows]
Z5, Z13, Z34, P13, P34 = 20, 21, 22, 25, 26
mid = sorted(m[0] for m in meta)[len(meta) // 2]


def edge(p, i):
    a = rows[i][1]
    return p / a - 1.0 - fee(a) / a


def solve(a, b):
    k = len(b)
    m = [[*a[i], b[i]] for i in range(k)]
    for c in range(k):
        p = max(range(c, k), key=lambda r_: abs(m[r_][c]))
        m[c], m[p] = m[p], m[c]
        pv = m[c][c] or 1e-9
        m[c] = [v / pv for v in m[c]]
        for r_ in range(k):
            if r_ != c and m[r_][c]:
                f = m[r_][c]
                m[r_] = [v - f * u for v, u in zip(m[r_], m[c], strict=False)]
    return [m[i][k] for i in range(k)]


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
        step = solve(h, g)
        w = [w[a] + step[a] for a in range(m)]

    def chance(i):
        x = fn(i)
        t = w[0] + sum(w[j + 1] * (x[j] - mu[j]) / sd[j] for j in range(k))
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, t))))

    return chance, w


def f_probit(i):
    r = rows[i][0]
    return [r[Z5], r[Z13], r[Z34], r[P13] - r[Z13], r[P34] - r[Z34], logit(rows[i][1])]


def f_off(i):
    return [logit(phi(rows[i][0][Z13])), logit(rows[i][1])]


def f_off_drift(i):
    r = rows[i][0]
    return [logit(phi(r[Z13])), r[P13] - r[Z13], r[P34] - r[Z34], logit(rows[i][1])]


def fit_k(idx):
    best, bk = 1e18, 1.0
    sub = idx[::6]
    for step in range(37):
        k = 0.7 + 0.05 * step
        ll = 0.0
        for i in sub:
            p = min(max(phi(rows[i][0][Z13] / k), 1e-4), 1 - 1e-4)
            ll -= math.log(p) if won[i] else math.log(1 - p)
        if ll < best:
            best, bk = ll, k
    return bk


_, wall = logfit([i for dd in days for i in by_day[dd]][::3], f_probit)
print("\nB. probit coefficients on the standardised inputs, fitted on all days (intercept, z_5m, z_13m, z_34m, drift_13m, drift_34m, logit_ask):", [round(x, 3) for x in wall])

NAMES = ("ridge18", "phi_z", "phi_k", "off", "off_drift", "probit")
picks = {v: {b: [] for b in (0.02, 0.05, 0.10)} for v in NAMES}
ks = []
for di, d in enumerate(days):
    if di < 3:
        continue
    train = [i for dd in days[:di] for i in by_day[dd]]
    test = by_day[d]
    sub = train[::3]
    rid = curvemodel.fit([(rows[i][0][:18], rows[i][1], rows[i][2]) for i in sub])
    k = fit_k(train)
    ks.append(k)
    c_off, _ = logfit(sub, f_off)
    c_od, _ = logfit(sub, f_off_drift)
    c_pr, _ = logfit(sub, f_probit)
    fns = {
        "ridge18": lambda i: curvemodel.predict(rid, rows[i][0][:18], rows[i][1]),  # noqa: B023
        "phi_z": lambda i: edge(phi(rows[i][0][Z13]), i),
        "phi_k": lambda i: edge(phi(rows[i][0][Z13] / k), i),  # noqa: B023
        "off": lambda i: edge(c_off(i), i),  # noqa: B023
        "off_drift": lambda i: edge(c_od(i), i),  # noqa: B023
        "probit": lambda i: edge(c_pr(i), i),  # noqa: B023
    }
    for v, fn in fns.items():
        for i in test:
            e = fn(i)
            for b in picks[v]:
                if e >= b:
                    picks[v][b].append(i)
print("\nC. the volatility scale k fitted per test day (Phi(z/k); k above 1 = the real spread is wider than the one-second rate says):", [round(x, 2) for x in ks])


def first(idx):
    seen = {}
    for i in sorted(idx, key=lambda i: meta[i][1]):
        seen.setdefault((meta[i][2], meta[i][3]), i)
    return list(seen.values())


def show(label, idx):
    if not idx:
        print(f"   {label:30s} n=0")
        return
    n = len(idx)
    nets = [rows[i][2] for i in idx]
    mean = sum(nets) / n
    sd = (sum((x - mean) ** 2 for x in nets) / max(n - 1, 1)) ** 0.5
    a = [rows[i][2] for i in idx if meta[i][0] <= mid]
    b = [rows[i][2] for i in idx if meta[i][0] > mid]
    print(f"   {label:30s} n={n:5d} net per $1 {mean:+.3f} +/- {sd / math.sqrt(n):.3f} won {100 * sum(won[i] for i in idx) / n:4.1f}% vs price {100 * sum(rows[i][1] for i in idx) / n:4.1f}% | halves {sum(a) / max(len(a), 1):+.3f} (n={len(a)}) / {sum(b) / max(len(b), 1):+.3f} (n={len(b)})")


print("\nD. picks at fixed edge bars, first per market side, net per $1 +/- one standard error")
for b in (0.02, 0.05, 0.10):
    print(f"  bar {b:.2f}")
    for v in NAMES:
        show(v, first(picks[v][b]))
print("\n   the plain curve's tails: buy the side where Phi(Z13) beats its ask by D (first per market side, every day, no fit)")
for lo, hi in ((0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.30), (0.30, 9.0)):
    show(f"D {lo:.2f}-{hi:.2f}", first([i for i in range(len(rows)) if lo <= phi(rows[i][0][Z13]) - rows[i][1] < hi]))
print("DONE")
