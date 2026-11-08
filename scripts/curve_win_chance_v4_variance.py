"""The win chance with the market's own variance ratio (2026-11-08, after two reviews of curve_win_chance_v3_checks.py).
Question: the variance ratio of the spot (realised variance over a span / (span x the one-second variance)) differs by market (0.8 to 1.6). Does z read in the market's own recent variance ratio (z_vr = Z13 / sqrt(VR), VR from that market's last 3 hours of ticks at the 300 s span, capped 0.5 to 2.5) pick better than the plain Phi(z), and does a logit(Phi(z)) offset model with the drift terms still carry signal?
A. What explains the ratio. CLAUDE.md rule 1 allows an asset name as a label only, so the ratio is split by the market's curve shape from marketprofile.profile_from over the same 3 hours: path efficiency (eff), typical minute (unit_bp), crossings of the open (cross); mean VR by tercile of each, and the rank correlation. The per-coin means are printed as a label next to the shape means, nothing more.
B. Walk-forward as in the other studies (each test day on the earlier days only, every third training moment, the first three days train only; rows from curvemodel.build_rows). Models: ridge18; phi_z (no fit); phi_vr (no fit, z_vr); phi_k_vr (Phi(z_vr / k), ONE global k fitted per training set, printed per day); off (logistic on logit(Phi(Z13)), logit(ask)); off_vr (the same with z_vr); off_vr_drift (logit(Phi(z_vr)), the 13 and 34 minute drift terms divided by sqrt(VR), logit(ask)); off_drift (the unadjusted version). Ridge 0.01 per training row, set before the results.
C. The coefficients per test day: the raw coefficient on logit(Phi(z)) (1.0 = the curve is right, under 1 = overconfident) and the standardised drift coefficients, to see whether the sign holds day by day.
D. Picks at fixed edge bars (0.02, 0.05, 0.10 per $1 expected net), first per market side: n, net per $1 with its standard error, win rate against the price, and for each half the net with ITS standard error (a half of the test days has a larger one).
E. Buying the side where Phi(z_vr) beats its ask by D (bands 0.05-0.10 ... 0.30+; the grid was chosen after the fade results in the v2 and v3 studies), first per market side, every row, no fit. Bands with n under 300 are marked: not readable.
Not covered: the 60-second settlement average, queue, sales before the result.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/curve_win_chance_v4_variance.py
"""

import collections
import itertools
import math
import os
import time

from kalshi_moonshot import config, curvemodel, db, marketprofile, variants
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
SQ2 = math.sqrt(2.0)
SPAN, HOURS = 300, 3
VR_LO, VR_HI = 0.5, 2.5


def phi(x):
    return 0.5 * (1.0 + math.erf(x / SQ2))


def logit(p):
    p = min(max(p, 0.001), 0.999)
    return math.log(p / (1 - p))


curvemodel.TRAIN_DAYS = (time.time() - 1_793_000_000) / 86400.0 + 0.5
meta = []
rows = curvemodel.build_rows(s, meta=meta, timed=True)
print("rows", len(rows), "market sides", len({(m[2], m[3]) for m in meta}))
G0 = int(min(m[0] for m in meta)) - 900 - HOURS * 3600 - 700
G1 = int(max(m[0] for m in meta)) + 2
SER = sorted({m[2].split("-")[0] for m in meta})
TICKS, GRID = {}, {}
for ser in SER:
    ix = variants.crypto_index(ser)
    r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, G0 - 60)).fetchall()
    ts = [float(x[0]) for x in r]
    vs = [float(x[1]) for x in r]
    TICKS[ser] = (ts, vs)
    g, j, last = [], 0, None
    for t in range(G0, G1):
        while j < len(ts) and ts[j] <= t:
            last = vs[j]
            j += 1
        g.append(math.log(last) if last else None)
    sq = [0.0]
    for a, b in itertools.pairwise(g):
        sq.append(sq[-1] + ((b - a) ** 2 if a is not None and b is not None else 0.0))
    GRID[ser] = (g, sq)


def vr_at(ser, t):
    """The variance ratio at the SPAN of the last HOURS hours before second t, or None."""
    g, sq = GRID[ser]
    k = int(t) - G0
    n = HOURS * 3600
    if k - n < 0 or k >= len(g):
        return None
    v1 = (sq[k] - sq[k - n]) / n
    if v1 <= 0:
        return None
    d = []
    for i in range(k - n, k - SPAN, SPAN):
        a, b = g[i], g[i + SPAN]
        if a is not None and b is not None:
            d.append(b - a)
    if len(d) < 10:
        return None
    return sum(x * x for x in d) / len(d) / (SPAN * v1)


# A. what explains the ratio
print("\nA. the variance ratio at 300 s against the curve shape of the same 3 hours (hourly samples, every coin)")
samples = []
t_start = int(min(m[0] for m in meta)) - 900
for ser in SER:
    ts, vs = TICKS[ser]
    for t in range(int(t_start // 3600 * 3600) + 3600, int(G1) - 1, 3600):
        v = vr_at(ser, t)
        p = marketprofile.profile_from(ts, vs, float(t), float(HOURS))
        if v is None or not p:
            continue
        samples.append((ser, v, p.get("eff"), p.get("unit_bp"), p.get("cross")))
print("   samples", len(samples))


def rank(xs):
    o = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    for k_, i in enumerate(o):
        r[i] = k_
    return r


def spearman(a, b):
    ra, rb = rank(a), rank(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=False))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else float("nan")


for idx, name in ((2, "eff (path efficiency)"), (3, "unit_bp (typical minute)"), (4, "cross (crossings of the open)")):
    ok = [x for x in samples if x[idx] is not None]
    vals = sorted(x[idx] for x in ok)
    c1, c2 = vals[len(vals) // 3], vals[2 * len(vals) // 3]
    terc = [[x[1] for x in ok if x[idx] <= c1], [x[1] for x in ok if c1 < x[idx] <= c2], [x[1] for x in ok if x[idx] > c2]]
    print(f"   {name:30s} mean VR by tercile low/mid/high: " + " / ".join(f"{sum(t)/len(t):.2f} (n={len(t)})" for t in terc) + f" | rank correlation with VR {spearman([x[1] for x in ok], [x[idx] for x in ok]):+.2f}")
print("   per-coin label (mean VR, mean eff), a label and nothing more:")
for ser in SER:
    c = [x for x in samples if x[0] == ser]
    if c:
        print(f"     {ser[2:6]:6s} VR {sum(x[1] for x in c) / len(c):.2f}  eff {sum(x[2] for x in c if x[2] is not None) / max(1, sum(1 for x in c if x[2] is not None)):.3f}")

# B. features
VRC = {}
VRN = [0]


def vr_for(i):
    c, e, tk, _, _ = meta[i]
    ser = tk.split("-")[0]
    t = c - 900.0 + e
    key = (ser, int(t // 600))
    if key not in VRC:
        v = vr_at(ser, key[1] * 600)
        VRC[key] = min(max(v, VR_LO), VR_HI) if v is not None else None
    v = VRC[key]
    if v is None:
        VRN[0] += 1
        return 1.0
    return v


VRS = [vr_for(i) for i in range(len(rows))]
print("\nmoments with no usable variance ratio (treated as 1.0):", VRN[0], "of", len(rows))
day = [int(m[0] // 86400) for m in meta]
days = sorted(set(day))
by_day = collections.defaultdict(list)
for i, d in enumerate(day):
    by_day[d].append(i)
won = [1 if r[2] > 0 else 0 for r in rows]
Z5, Z13, Z34, P13, P34 = 20, 21, 22, 25, 26
mid = sorted(m[0] for m in meta)[len(meta) // 2]


def zvr(i):
    return rows[i][0][Z13] / math.sqrt(VRS[i])


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

    return chance, w, sd


def f_off(i):
    return [logit(phi(rows[i][0][Z13])), logit(rows[i][1])]


def f_off_vr(i):
    return [logit(phi(zvr(i))), logit(rows[i][1])]


def f_off_drift(i):
    r = rows[i][0]
    return [logit(phi(r[Z13])), r[P13] - r[Z13], r[P34] - r[Z34], logit(rows[i][1])]


def f_off_vr_drift(i):
    r = rows[i][0]
    q = math.sqrt(VRS[i])
    return [logit(phi(zvr(i))), (r[P13] - r[Z13]) / q, (r[P34] - r[Z34]) / q, logit(rows[i][1])]


def fit_k(idx):
    best, bk = 1e18, 1.0
    sub = idx[::6]
    for step in range(27):
        k = 0.7 + 0.05 * step
        ll = 0.0
        for i in sub:
            p = min(max(phi(zvr(i) / k), 1e-4), 1 - 1e-4)
            ll -= math.log(p) if won[i] else math.log(1 - p)
        if ll < best:
            best, bk = ll, k
    return bk


NAMES = ("ridge18", "phi_z", "phi_vr", "phi_k_vr", "off", "off_vr", "off_drift", "off_vr_drift")
BARS = (0.02, 0.05, 0.10)
picks = {v: {b: [] for b in BARS} for v in NAMES}
ks = []
coefs = collections.defaultdict(list)
for di, d in enumerate(days):
    if di < 3:
        continue
    train = [i for dd in days[:di] for i in by_day[dd]]
    test = by_day[d]
    sub = train[::3]
    rid = curvemodel.fit([(rows[i][0][:18], rows[i][1], rows[i][2]) for i in sub])
    k = fit_k(train)
    ks.append(round(k, 2))
    c_off, w_off, sd_off = logfit(sub, f_off)
    c_ov, w_ov, sd_ov = logfit(sub, f_off_vr)
    c_od, w_od, sd_od = logfit(sub, f_off_drift)
    c_ovd, w_ovd, sd_ovd = logfit(sub, f_off_vr_drift)
    coefs["off: raw coef on logit(Phi(z))"].append(round(w_off[1] / sd_off[0], 2))
    coefs["off_vr: raw coef on logit(Phi(z_vr))"].append(round(w_ov[1] / sd_ov[0], 2))
    coefs["off_drift: raw coef on logit(Phi(z))"].append(round(w_od[1] / sd_od[0], 2))
    coefs["off_drift: std coef drift_13m"].append(round(w_od[2], 3))
    coefs["off_drift: std coef drift_34m"].append(round(w_od[3], 3))
    coefs["off_vr_drift: raw coef on logit(Phi(z_vr))"].append(round(w_ovd[1] / sd_ovd[0], 2))
    coefs["off_vr_drift: std coef drift_13m"].append(round(w_ovd[2], 3))
    coefs["off_vr_drift: std coef drift_34m"].append(round(w_ovd[3], 3))
    fns = {
        "ridge18": lambda i: curvemodel.predict(rid, rows[i][0][:18], rows[i][1]),  # noqa: B023
        "phi_z": lambda i: edge(phi(rows[i][0][Z13]), i),
        "phi_vr": lambda i: edge(phi(zvr(i)), i),
        "phi_k_vr": lambda i: edge(phi(zvr(i) / k), i),  # noqa: B023
        "off": lambda i: edge(c_off(i), i),  # noqa: B023
        "off_vr": lambda i: edge(c_ov(i), i),  # noqa: B023
        "off_drift": lambda i: edge(c_od(i), i),  # noqa: B023
        "off_vr_drift": lambda i: edge(c_ovd(i), i),  # noqa: B023
    }
    for v, fn in fns.items():
        for i in test:
            e = fn(i)
            for b in BARS:
                if e >= b:
                    picks[v][b].append(i)
print("\nB/C. the one global k of phi_k_vr per test day:", ks)
print("C. coefficients per test day (days after the first three):")
for name, vals in coefs.items():
    print(f"   {name:46s}", vals)


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
    sd = (sum((x - m) ** 2 for x in xs) / max(n - 1, 1)) ** 0.5
    return f"{m:+.3f} +/- {sd / math.sqrt(n):.3f} (n={n})"


def show(label, idx):
    if not idx:
        print(f"   {label:16s} n=0")
        return
    nets = [rows[i][2] for i in idx]
    a = [rows[i][2] for i in idx if meta[i][0] <= mid]
    b = [rows[i][2] for i in idx if meta[i][0] > mid]
    print(f"   {label:16s} net per $1 {ms(nets)} won {100 * sum(won[i] for i in idx) / len(idx):4.1f}% vs price {100 * sum(rows[i][1] for i in idx) / len(idx):4.1f}% | first half {ms(a)} | second half {ms(b)}")


print("\nD. picks at fixed edge bars, first per market side (net per $1 +/- one standard error, per half too)")
for b in BARS:
    print(f"  bar {b:.2f}")
    for v in NAMES:
        show(v, first(picks[v][b]))
print("\nE. buying the side where Phi(z_vr) beats its ask by D (first per market side, every row, no fit; the D grid was chosen after the fade results)")
for lo, hi in ((0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.30), (0.30, 9.0)):
    f = first([i for i in range(len(rows)) if lo <= phi(zvr(i)) - rows[i][1] < hi])
    show(f"D {lo:.2f}-{hi:.2f}", f)
    if len(f) < 300:
        print("                  ^ n under 300: not readable")
print("DONE")
