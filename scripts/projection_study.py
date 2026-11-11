"""Project the curve to the close from several stages of its history, weighed by time left and cushion (2026-11-11, user: "we're supposed to be looking at different stages of history and weighing them depending on the cushion and time left ... using logarithmic calculus we should be able to get a general idea of where the curve will be at the end"; the live curve model reads 5, 13 and 34 minutes with one fixed set of weights and no time left).
Reading, YES side, at 600 / 420 / 300 / 180 / 120 / 60 s left, on settled 15-minute windows with a spot index (1 in STRIDE):
  the cushion now (log spot minus log reference: the strike for the settlement-source indexes, the index's own window-open value for a proxy feed);
  at each stage m = 2, 5, 13, 34, 60 minutes a parabola through the last m minutes of the log spot gives a slope b and a curvature c (calculus on the log price: b is the rate of change, c the change of that rate); the stage's projected CHANGE by the close is b*Te (linear) or b*Te + c*Te^2/2 (quadratic), Te = the time left but no longer than the stage itself (a 2-minute stage is not carried 10 minutes);
  everything in the market's own units: divided by the typical minute (rms one-second move of the last 900 s times sqrt(60)) times sqrt(minutes left), so the cushion term z0 is the curve's usual reading and each stage adds or removes from it.
Weighing: a separate logistic fit per feed kind, time left and cushion band (|z0| under 0.5, 0.5 to 1.5, 1.5 and up), so each stage's weight depends on the time left and the cushion. Walk-forward by UTC day: a day is predicted from fits on all earlier days (at least 150 readings in the cell, else the cell falls back to the price).
Scored on the same out-of-sample readings by Brier (lower is better): the contract's mid; M0 the cushion alone; MA cushion + linear projections; MB cushion + quadratic projections; MP the mid recalibrated; MC the mid + cushion + linear projections. The bar (studies/results/2026-11-11-implied-value.txt): a projection is an edge only if MC beats the mid, i.e. it adds to what the price already says. Grid and model forms fixed before results.
    scripts/cloud_job.sh scripts/projection_study.py STRIDE=3
"""

import bisect
import calendar
import collections
import math
import os
import time as _time

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store

os.nice(10)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
STRIDE = int(os.environ.get("STRIDE", "3"))
SINCE_S = int(os.environ.get("SINCE_S", str(calendar.timegm((2026, 10, 28, 0, 0, 0)))))
LEFTS = (600, 420, 300, 180, 120, 60)
STAGES = (2, 5, 13, 34, 60)
RIDGE = 2.0


def parabola(xs, ys):
    """Least-squares y = a + b x + c2 x^2; returns (a, b, 2*c2): value, slope and curvature at x = 0."""
    n = len(xs)
    sx = sum(xs); sx2 = sum(x * x for x in xs); sx3 = sum(x ** 3 for x in xs); sx4 = sum(x ** 4 for x in xs)
    sy = sum(ys); sxy = sum(x * y for x, y in zip(xs, ys)); sx2y = sum(x * x * y for x, y in zip(xs, ys))
    A = [[n, sx, sx2, sy], [sx, sx2, sx3, sxy], [sx2, sx3, sx4, sx2y]]
    sol = solve(A)
    return None if sol is None else (sol[0], sol[1], 2.0 * sol[2])


def solve(A):
    n = len(A)
    A = [row[:] for row in A]
    for i in range(n):
        p = max(range(i, n), key=lambda r: abs(A[r][i]))
        if abs(A[p][i]) < 1e-14:
            return None
        A[i], A[p] = A[p], A[i]
        for r in range(n):
            if r != i:
                f = A[r][i] / A[i][i]
                if f:
                    for c in range(i, n + 1):
                        A[r][c] -= f * A[i][c]
    return [A[i][n] / A[i][i] for i in range(n)]


def logistic_fit(X, y):
    """Ridge logistic regression by Newton steps; X rows already include the constant."""
    p = len(X[0])
    w = [0.0] * p
    for _ in range(7):
        g = [0.0] * p
        H = [[0.0] * p for _ in range(p)]
        for row, t in zip(X, y):
            z = sum(a * b for a, b in zip(w, row))
            z = max(-30.0, min(30.0, z))
            mu = 1.0 / (1.0 + math.exp(-z))
            d = mu - t
            v = max(mu * (1.0 - mu), 1e-6)
            for i in range(p):
                ri = row[i]
                g[i] += d * ri
                Hi = H[i]
                rv = ri * v
                for j in range(i, p):
                    Hi[j] += rv * row[j]
        for i in range(p):
            for j in range(i):
                H[i][j] = H[j][i]
            if i:  # no penalty on the constant
                g[i] += RIDGE * w[i]
                H[i][i] += RIDGE
        step = solve([H[i] + [g[i]] for i in range(p)])
        if step is None:
            break
        w = [a - b for a, b in zip(w, step)]
        if max(abs(x) for x in step) < 1e-5:
            break
    return w


def predict(w, row):
    z = max(-30.0, min(30.0, sum(a * b for a, b in zip(w, row))))
    return 1.0 / (1.0 + math.exp(-z))


wins = st.db.execute("SELECT ticker, close_ts, strike, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (SINCE_S,)).fetchall()
R = []  # (day, kind, L, band, z0, [lin...], [quad...], mid, won)
used = 0
t_start = _time.time()
for k, w in enumerate(wins):
    ser = w["ticker"].split("-")[0]
    crypto = variants.crypto_index(ser)
    ix = crypto or variants.biquote_index(ser)
    if not ser.endswith("15M") or not ix or k % STRIDE:
        continue
    close = float(w["close_ts"])
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, close - 4320, close)).fetchall()
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], close - 900, close)).fetchall()
    if len(tk) < 2500 or len(q) < 5 or float(tk[0][0]) > close - 4250:
        continue
    T_ = [float(x[0]) for x in tk]
    base = int(close) - 4300
    g = []
    j = 0
    last = None
    for t in range(base, int(close) + 1):
        while j < len(T_) and T_[j] <= t:
            last = float(tk[j][1])
            j += 1
        g.append(math.log(last) if last else None)
    if crypto:
        if not w["strike"] or float(w["strike"]) <= 0:
            continue
        ref = math.log(float(w["strike"]))
    else:
        a = bisect.bisect_left(T_, close - 905.0)
        if a >= len(T_) or T_[a] > close - 880.0:
            continue
        ref = math.log(float(tk[a][1]))
    qt = [float(x[0]) for x in q]
    won = 1 if w["result"] == "yes" else 0
    ok = False
    for L in LEFTS:
        kk = int(close) - L - base
        if g[kk] is None or g[kk - 3600] is None:
            continue
        i = bisect.bisect_right(qt, close - L) - 1
        if i < 0 or (close - L) - qt[i] > 20:
            continue
        seg = g[kk - 900 : kk + 1]
        if any(x is None for x in seg):
            continue
        u = math.sqrt(sum((b - a) ** 2 for a, b in zip(seg, seg[1:])) / 900.0) * math.sqrt(60.0)
        if u <= 0:
            continue
        Tm = L / 60.0
        scale = u * math.sqrt(Tm)
        z0 = (g[kk] - ref) / scale
        lin, quad = [], []
        bad = False
        for m in STAGES:
            n = 30
            xs = [-(m * (n - 1 - a) / (n - 1)) for a in range(n)]  # minutes before now, 0 = now
            ys = []
            for x in xs:
                v = g[kk + int(round(x * 60))]
                if v is None:
                    bad = True
                    break
                ys.append(v - g[kk])
            if bad:
                break
            fit = parabola(xs, ys)
            if fit is None:
                bad = True
                break
            _, b, c = fit
            Te = min(Tm, float(m))
            lin.append(max(-6.0, min(6.0, b * Te / scale)))
            quad.append(max(-6.0, min(6.0, (b * Te + 0.5 * c * Te * Te) / scale)))
        if bad:
            continue
        mid = (float(q[i][1]) + float(q[i][2])) / 2.0
        z0c = max(-6.0, min(6.0, z0))
        band = 0 if abs(z0c) < 0.5 else 1 if abs(z0c) < 1.5 else 2
        R.append((int(close // 86400), "settlement" if crypto else "proxy", L, band, z0c, lin, quad, min(max(mid, 0.01), 0.99), won))
        ok = True
    used += ok
    if used and used % 400 == 0 and ok:
        print(f"  {used} windows, {len(R)} readings, {_time.time() - t_start:.0f}s", flush=True)
print(f"windows {used} (1 in {STRIDE}), readings {len(R)}")

MODELS = {
    "M0 cushion": lambda r: [1.0, r[4]],
    "MA cushion + linear": lambda r: [1.0, r[4], *r[5]],
    "MB cushion + quadratic": lambda r: [1.0, r[4], *r[6]],
    "MP mid recalibrated": lambda r: [1.0, math.log(r[7] / (1 - r[7]))],
    "MC mid + cushion + linear": lambda r: [1.0, math.log(r[7] / (1 - r[7])), r[4], *r[5]],
}
BANDS = ("thin (under 0.5)", "medium (0.5 to 1.5)", "thick (1.5 and up)")
for kind in ("settlement", "proxy"):
    K = [r for r in R if r[1] == kind]
    if len(K) < 2000:
        print(f"\\n=== feed kind {kind}: {len(K)} readings, too few")
        continue
    days = sorted({r[0] for r in K})
    preds = collections.defaultdict(dict)  # model -> id(reading) -> p
    lastw = {}
    for L in LEFTS:
        for band in range(3):
            C = [r for r in K if r[2] == L and r[3] == band]
            for d in days:
                past = [r for r in C if r[0] < d]
                today = [r for r in C if r[0] == d]
                if not today:
                    continue
                for name, fx in MODELS.items():
                    if len(past) < 150:
                        for r in today:
                            preds[name][id(r)] = r[7]
                        continue
                    wv = logistic_fit([fx(r) for r in past], [r[8] for r in past])
                    lastw[(name, L, band)] = (wv, len(past))
                    for r in today:
                        preds[name][id(r)] = predict(wv, fx(r))
    scored = [r for r in K if id(r) in preds["M0 cushion"]]
    print(f"\\n=== feed kind {kind}: {len(K)} readings, {len(scored)} scored out of sample over {len(days)} days")
    print("  Brier by time left: n | the contract's mid | " + " | ".join(MODELS))
    for L in (*LEFTS, None):
        S = [r for r in scored if L is None or r[2] == L]
        if not S:
            continue
        br = lambda f: sum((f(r) - r[8]) ** 2 for r in S) / len(S)  # noqa: E731
        print(f"    {'all' if L is None else str(L) + 's':5s} n {len(S):5d} | {br(lambda r: r[7]):.4f} | " + " | ".join(f"{br(lambda r, nm=nm: preds[nm][id(r)]):.4f}" for nm in MODELS))
    print("  Brier by cushion band (all times left): the mid | " + " | ".join(MODELS))
    for band in range(3):
        S = [r for r in scored if r[3] == band]
        if S:
            br = lambda f: sum((f(r) - r[8]) ** 2 for r in S) / len(S)  # noqa: E731
            print(f"    {BANDS[band]:20s} n {len(S):5d} | {br(lambda r: r[7]):.4f} | " + " | ".join(f"{br(lambda r, nm=nm: preds[nm][id(r)]):.4f}" for nm in MODELS))
    half = days[len(days) // 2]
    for lab, S in (("first half of the days", [r for r in scored if r[0] < half]), ("second half", [r for r in scored if r[0] >= half])):
        if S:
            br = lambda f: sum((f(r) - r[8]) ** 2 for r in S) / len(S)  # noqa: E731
            print(f"  {lab}: n {len(S)} | mid {br(lambda r: r[7]):.4f} | " + " | ".join(f"{nm.split()[0]} {br(lambda r, nm=nm: preds[nm][id(r)]):.4f}" for nm in MODELS))
    print("  the weights of the last fit of MA (cushion, then the linear projection of each stage " + ", ".join(f"{m}m" for m in STAGES) + "):")
    for L in LEFTS:
        for band in range(3):
            got = lastw.get(("MA cushion + linear", L, band))
            if got:
                wv, n = got
                print(f"    {L:3d}s left, cushion {BANDS[band]:20s} (n {n:4d}): cushion {wv[1]:+.2f} | " + " ".join(f"{m}m {x:+.2f}" for m, x in zip(STAGES, wv[2:])))
print("DONE")
