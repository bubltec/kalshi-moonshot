"""Confidence score study: does a curve-feature model know something the market price does not? Read-only, pure Python (the host has no numpy).

Question: at 5, 3 and 1.5 minutes before the close of a crypto 15-minute market, does a logistic model on the curve (distance to the strike in typical minutes, 60 s drift, bounce off the 5-minute extremes, path efficiency, 15-minute range and position, and the hour: the hour's move, where the strike and the spot sit in the hour's range) predict the settlement better than the market's own price does?
Method: one row per settled window and mark. Targets: the window settled YES. Baselines, fit on the earlier days only: M0 the market mid price (a logistic on logit(mid) to allow for a miscalibrated price), M1 the digital price from the spot's own variance (the normal cdf of ln(spot/strike) over sigma * sqrt(seconds left)). Model M2: logit(mid) plus the curve features, ridge-penalised. Walk-forward: train on the days before each of THREE FOLDS, score on the fold. Reported: out-of-sample log loss for each model, and the result of buying the side the model likes when it beats the ask by at least EDGE, after the fee, held to settlement.
No asset name enters a feature (CLAUDE.md): every distance is in the market's own typical minutes.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/confidence_study.py
"""

import array
import bisect
import math
import os
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshiws import index_for_series
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

DAYS = float(os.environ.get("DAYS", "8"))
MARKS = (300, 180, 90)
EDGES = (0.03, 0.06, 0.10)
RIDGE = 1.0

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
t_now = time.time()
wins = main.db.execute(
    "SELECT ticker, series, close_ts, result, strike FROM windows WHERE result IN ('yes','no') AND strike IS NOT NULL AND close_ts>? ORDER BY close_ts", (t_now - DAYS * 86400,)
).fetchall()
series = sorted({w["series"] for w in wins if w["series"] and w["series"].startswith("KX") and w["series"][2:5] in ("BTC", "ETH", "SOL", "XRP", "DOG")})
print("windows", len(wins), "series", series)


def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def logit(p):
    p = min(1 - 1e-4, max(1e-4, p))
    return math.log(p / (1 - p))


rows = []  # (close_ts, y, left, ask_yes, bid_yes, features dict)
for ser in series:
    idx = index_for_series(ser)
    if not idx:
        continue
    d = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (idx, t_now - DAYS * 86400 - 4500)).fetchall()
    T = array.array("d", (float(r[0]) for r in d))
    V = array.array("d", (float(r[1]) for r in d))
    del d
    for w in (x for x in wins if x["series"] == ser):
        close, strike = float(w["close_ts"]), float(w["strike"])
        y = 1 if w["result"] == "yes" else 0
        quotes = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 400, close)).fetchall()
        qt = [float(q["ts"]) for q in quotes]
        for left in MARKS:
            t = close - left
            j = bisect.bisect_right(T, t) - 1
            lo900, lo300, lo60, lo3600 = (bisect.bisect_right(T, t - x) for x in (900, 300, 60, 3600))
            if j < 0 or t - T[j] > 10 or j - lo900 < 600 or lo3600 >= j or T[lo3600] < t - 3700:
                continue
            qi = bisect.bisect_right(qt, t) - 1
            if qi < 0 or t - qt[qi] > 15 or quotes[qi]["bid"] is None or quotes[qi]["ask"] is None:
                continue
            bid, ask = float(quotes[qi]["bid"]), float(quotes[qi]["ask"])
            if not (0 < ask <= 1) or bid < 0:
                continue
            seq = V[lo900 : j + 1]
            steps = [math.log(b / a) for a, b in zip(seq, seq[1:]) if a > 0 and b > 0]
            if len(steps) < 300:
                continue
            sig1 = math.sqrt(sum(x * x for x in steps) / len(steps))
            unit = sig1 * math.sqrt(60.0)
            if unit <= 0:
                continue
            spot = V[j]
            w5 = V[lo300 : j + 1]
            h = V[lo3600 : j + 1]
            lo15, hi15 = min(seq), max(seq)
            lo_h, hi_h = min(h), max(h)
            path5 = sum(abs(math.log(b / a)) for a, b in zip(w5, w5[1:]) if a > 0 and b > 0)
            zk = math.log(spot / strike) / (sig1 * math.sqrt(left))
            f = {
                "z_tm": math.log(spot / strike) / unit,
                "d60": math.log(spot / V[lo60]) / unit if lo60 <= j else 0.0,
                "up5": math.log(max(w5) / spot) / unit,
                "dn5": math.log(spot / min(w5)) / unit,
                "eff5": (abs(math.log(w5[-1] / w5[0])) / path5) if path5 > 0 else 0.0,
                "amp15": math.log(hi15 / lo15) / unit,
                "pos15": (spot - lo15) / (hi15 - lo15) if hi15 > lo15 else 0.5,
                "h_move": math.log(spot / h[0]) / unit,
                "h_pos": (spot - lo_h) / (hi_h - lo_h) if hi_h > lo_h else 0.5,
                "h_strike": (strike - lo_h) / (hi_h - lo_h) if hi_h > lo_h else 0.5,
                "left": left / 60.0,
            }
            rows.append((close, y, left, ask, bid, f, logit((ask + bid) / 2.0), ncdf(zk)))
    del T, V
print("rows", len(rows))
rows.sort(key=lambda r: r[0])

NAMES = ["z_tm", "d60", "up5", "dn5", "eff5", "amp15", "pos15", "h_move", "h_pos", "h_strike", "left"]


def fit(X, y, ridge=RIDGE, iters=12):
    n, k = len(X), len(X[0])
    w = [0.0] * k
    for _ in range(iters):
        g = [0.0] * k
        H = [[0.0] * k for _ in range(k)]
        for xi, yi in zip(X, y):
            z = sum(a * b for a, b in zip(w, xi))
            p = 1.0 / (1.0 + math.exp(-max(-30, min(30, z))))
            r = p - yi
            wt = p * (1 - p) + 1e-6
            for a in range(k):
                g[a] += r * xi[a]
                xa = wt * xi[a]
                Ha = H[a]
                for b in range(a, k):
                    Ha[b] += xa * xi[b]
        for a in range(k):
            for b in range(a):
                H[a][b] = H[b][a]
            if a > 0:
                H[a][a] += ridge
                g[a] += ridge * w[a]
        # solve H dw = g by Gauss-Jordan
        A = [H[a][:] + [g[a]] for a in range(k)]
        for c in range(k):
            piv = max(range(c, k), key=lambda r_: abs(A[r_][c]))
            A[c], A[piv] = A[piv], A[c]
            pv = A[c][c] or 1e-9
            A[c] = [x / pv for x in A[c]]
            for r_ in range(k):
                if r_ != c:
                    fac = A[r_][c]
                    if fac:
                        A[r_] = [x - fac * z for x, z in zip(A[r_], A[c])]
        dw = [A[a][k] for a in range(k)]
        w = [a - b for a, b in zip(w, dw)]
        if max(abs(x) for x in dw) < 1e-6:
            break
    return w


def predict(w, xi):
    z = sum(a * b for a, b in zip(w, xi))
    return 1.0 / (1.0 + math.exp(-max(-30, min(30, z))))


def loss(ps, ys):
    return -sum(math.log(max(1e-6, p if y else 1 - p)) for p, y in zip(ps, ys)) / len(ys)


# standardise the curve features on the whole sample (no outcome in it)
mu = {n: sum(r[5][n] for r in rows) / len(rows) for n in NAMES}
sd = {n: (sum((r[5][n] - mu[n]) ** 2 for r in rows) / len(rows)) ** 0.5 or 1.0 for n in NAMES}


def x0(r):
    return [1.0, r[6]]


def x1(r):
    return [1.0, logit(r[7])]


def x2(r):
    return [1.0, r[6]] + [(r[5][n] - mu[n]) / sd[n] for n in NAMES]


days = sorted({int(r[0] // 86400) for r in rows})
folds = [days[-3:-2], days[-2:-1], days[-1:]]
print("days", len(days), "test folds (day index)", folds)
tot = {"M0": [], "M1": [], "M2": []}
ev = {e: [] for e in EDGES}
for fd in folds:
    tr = [r for r in rows if int(r[0] // 86400) < fd[0]]
    te = [r for r in rows if int(r[0] // 86400) in fd]
    if len(tr) < 500 or not te:
        continue
    ytr = [r[1] for r in tr]
    w0, w1, w2 = fit([x0(r) for r in tr], ytr), fit([x1(r) for r in tr], ytr), fit([x2(r) for r in tr], ytr)
    yte = [r[1] for r in te]
    p0, p1, p2 = [predict(w0, x0(r)) for r in te], [predict(w1, x1(r)) for r in te], [predict(w2, x2(r)) for r in te]
    print(f"fold day {fd[0]}: train {len(tr)} test {len(te)} | log loss market {loss(p0, yte):.4f} digital {loss(p1, yte):.4f} model {loss(p2, yte):.4f}")
    for a, b in zip(("M0", "M1", "M2"), (p0, p1, p2)):
        tot[a] += list(zip(b, yte))
    for e in EDGES:
        for r, p in zip(te, p2):
            # buy YES at the ask or NO at 1 - bid when the model beats that price by e
            if p - r[3] >= e:
                ev[e].append(((1.0 if r[1] else 0.0) - r[3] - fee_per_contract(r[3]), r[0]))
            elif (1 - p) - (1 - r[4]) >= e:
                ev[e].append(((0.0 if r[1] else 1.0) - (1 - r[4]) - fee_per_contract(1 - r[4]), r[0]))
print("pooled out-of-sample log loss:", {k: round(loss([p for p, _ in v], [y for _, y in v]), 4) for k, v in tot.items() if v}, "rows", len(tot["M0"]))
print("coefficients (standardised features, last fit):", {n: round(c, 3) for n, c in zip(["const", "logit_mid"] + NAMES, w2)})
for e in EDGES:
    g = ev[e]
    if g:
        half = sorted(t for _, t in g)[len(g) // 2]
        a = [v for v, t in g if t <= half]
        b = [v for v, t in g if t > half]
        print(f"model beats the price by >= {e:.2f}: {len(g)} buys, EV {sum(v for v, _ in g) / len(g):+.4f}/contract; earlier half {sum(a) / max(len(a), 1):+.4f} (n={len(a)}), later half {sum(b) / max(len(b), 1):+.4f} (n={len(b)})")
print("DONE")
