"""CASE-BASED version: forecast each moment from the k most similar PAST moments (distance over the six look-back slices), blend it with the regression, score on later windows. Original doc follows.\nFREEZE / REVIEW modes (env FC_MODE, FC_FILE): FC_MODE=freeze fits on everything recorded so far and writes the weights, the coin scales and the cutoff to FILE; FC_MODE=review loads FILE and scores only windows that closed after the cutoff,
so a day later it says whether the forecasts were right (forecast move vs the move that followed, and what buying on them would have returned). Default FC_MODE is the plain study.
    docker compose exec -T -e FC_MODE=freeze -e FC_FILE=/data/forecast_h2.json -e H=2 bot python - < scripts/multiwindow_short_study.py
Short-horizon version (forecast the next H minutes of the market price, not the result; non-overlapping look-back slices). Original: Does the settlement index's move over several look-back windows (3, 7, 15, 30, 60, 120 minutes) forecast a 15-minute crypto favorite beyond its price? Replayed from the tape. Places nothing.

Every 30 s from 5 minutes into a window to 2 minutes before it closes, for the side the market leans toward (mid 55c or more, ask up to 92c, spread 8c or less):
  z_w     the index's log move over the last w minutes, signed toward that side, divided by the usual size of a w-minute move for that coin (so each window is comparable)
  y       win (1/0) minus ask minus the buy fee: the profit per contract of buying at the ask and holding
Least squares of y on the six z_w (clustered by 15-minute slot, since the coins move together). A coefficient says how much that window moves the edge per unit of its own normal size, with the other windows held fixed.
Out of sample: fit on the earlier half of the windows, then buy only the test moments where the fitted edge is above zero (and above 1c); compare with buying every test moment.
    docker compose exec -T bot python - < scripts/multiwindow_forecast_study.py
"""
import bisect
import json
import os
import time
import math
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.kalshiws import index_for_series
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
CRYPTO = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
RUN_AS, FILE = os.environ.get('FC_MODE', 'study'), os.environ.get('FC_FILE', '')
WINDOWS = (3, 7, 15, 30, 60, 120)
EDGES = (0,) + WINDOWS  # slice i covers EDGES[i]..EDGES[i+1] minutes back
HORIZON = int(__import__('os').environ.get('H', '2'))
ticks = {}
sigma = {}
for ix in {index_for_series(x) for x in CRYPTO}:
    r = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND value > 0 ORDER BY ts", (ix,)).fetchall()
    ticks[ix] = ([x[0] for x in r], [x[1] for x in r])
    t0, t1 = ticks[ix][0][0], ticks[ix][0][-1]
    sigma[ix] = {}
    for w in WINDOWS:
        rets, t = [], t0 + 7300
        while t < t1:
            a, b = bisect.bisect_right(ticks[ix][0], t) - 1, bisect.bisect_right(ticks[ix][0], t - w * 60) - 1
            rets.append(math.log(ticks[ix][1][a] / ticks[ix][1][b]))
            t += 120
        sigma[ix][w] = math.sqrt(sum(x * x for x in rets) / len(rets))


FROZEN = None
if RUN_AS == 'review':
    FROZEN = json.load(open(FILE))
    sigma = {ix: {int(w): v for w, v in d.items()} for ix, d in FROZEN['sigma'].items()}
    HORIZON = FROZEN['horizon']


def value(ix, t):
    ts, v = ticks[ix]
    i = bisect.bisect_right(ts, t) - 1
    return v[i] if i >= 0 and t - ts[i] < 30 else None


def solve(a, b):
    n = len(a)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c], strict=True)]
    return [m[i][n] / m[i][i] for i in range(n)]


def inverse(a):
    n = len(a)
    cols = [solve(a, [1.0 if i == j else 0.0 for i in range(n)]) for j in range(n)]
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def ols(rows):
    k = len(WINDOWS) + 1
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for r in rows:
        x = [1.0] + r["z"]
        for i in range(k):
            xty[i] += x[i] * r["y"]
            for j in range(k):
                xtx[i][j] += x[i] * x[j]
    beta = solve(xtx, xty)
    inv = inverse(xtx)
    meat = [[0.0] * k for _ in range(k)]
    score = defaultdict(lambda: [0.0] * k)
    for r in rows:
        x = [1.0] + r["z"]
        e = r["y"] - sum(b * v for b, v in zip(beta, x, strict=True))
        for i in range(k):
            score[r["cl"]][i] += x[i] * e
    for sc in score.values():
        for i in range(k):
            for j in range(k):
                meat[i][j] += sc[i] * sc[j]
    cov = [[sum(inv[i][a] * meat[a][b] * inv[b][j] for a in range(k) for b in range(k)) for j in range(k)] for i in range(k)]
    return beta, [math.sqrt(max(cov[i][i], 0.0)) for i in range(k)]


rows = []
for m in main.db.execute(f"SELECT ticker, series, result, close_ts FROM windows WHERE series IN ({','.join('?' * len(CRYPTO))}) AND result IN ('yes','no')" + (" AND close_ts > %s" % FROZEN['cutoff'] if FROZEN else ""), CRYPTO).fetchall():
    ix = index_for_series(m["series"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (m["ticker"],)).fetchall()
    if len(q) < 60 or ix is None:
        continue
    qts = [r[0] for r in q]
    close, t = m["close_ts"], m["close_ts"] - 900 + 300
    while t < close - 120:
        i = bisect.bisect_right(qts, t) - 1
        t += 30
        if i < 0:
            continue
        by, ay = q[i][1], q[i][2]
        side = "yes" if (by + ay) / 2 >= 0.5 else "no"
        a, b = (ay, by) if side == "yes" else (1 - by, 1 - ay)
        if not (0.55 <= (a + b) / 2 and a <= 0.92 and a - b <= 0.08):
            continue
        now = value(ix, qts[i])
        sign = 1.0 if side == "yes" else -1.0
        zs = []
        for lo, hi in zip(EDGES, EDGES[1:], strict=False):
            a0, b0 = value(ix, qts[i] - lo * 60), value(ix, qts[i] - hi * 60)
            if a0 is None or b0 is None:
                break
            zs.append(sign * math.log(a0 / b0) / (sigma[ix][hi] * math.sqrt((hi - lo) / hi)))
        if len(zs) < len(WINDOWS):
            continue
        j = bisect.bisect_right(qts, qts[i] + HORIZON * 60) - 1
        if j <= i or qts[j] > close - 5:
            continue
        fb = q[j][1] if side == "yes" else 1 - q[j][2]
        fm = (q[j][1] + q[j][2]) / 2 if side == "yes" else 1 - (q[j][1] + q[j][2]) / 2
        net = fb - fee(fb) - a - fee(a)
        rows.append({"cl": m["close_ts"], "z": zs, "y": fm - (a + b) / 2, "ret": net / (a + fee(a)), "t": m["close_ts"]})

import heapq
K = int(os.environ.get("K", "100"))
cuts = sorted({r["t"] for r in rows})
cut = cuts[len(cuts) // 2]
train, test = [r for r in rows if r["t"] <= cut], [r for r in rows if r["t"] > cut]
bt, _ = ols(train)
memory = train[::2]  # every other past moment: neighbours of neighbours add little and pure Python is slow
print(f"{len(rows):,} moments; memory {len(memory):,} past moments, scored on {len(test):,} later ones; horizon {HORIZON} min; k = {K}")


def reg(r):
    return bt[0] + sum(b * v for b, v in zip(bt[1:], r["z"], strict=True))


def knn(r):
    near = heapq.nsmallest(K, memory, key=lambda m: sum((a - b) ** 2 for a, b in zip(m["z"], r["z"], strict=True)))
    return statistics.mean(m["y"] for m in near)


for r in test[::1]:
    r["reg"], r["knn"] = reg(r), knn(r)
    r["mix"] = 0.5 * r["reg"] + 0.5 * r["knn"]


def corr(key):
    xs, ys = [r[key] for r in test], [r["y"] for r in test]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))


def line(label, g):
    if len(g) < 30:
        print(f"  {label:30} only {len(g)}")
        return
    per = defaultdict(list)
    for r in g:
        per[r["cl"]].append(r["ret"])
    mm = [sum(v) / len(v) for v in per.values()]
    sd = math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm)) if len(mm) > 1 else float("nan")
    print(f"  {label:30} n={len(g):5} ({len(per):3} windows)  realized move {100 * statistics.mean(r['y'] for r in g):+5.2f}c  round trip after fees {statistics.mean(r['ret'] for r in g):+6.1%} +-{100 * sd:.1f}")


for key, name in (("reg", "regression"), ("knn", "similar past moments"), ("mix", "half and half")):
    print(f"\n{name}: correlation of forecast with the move that followed {corr(key):+.3f}")
    for c in (0.0, 0.005, 0.01):
        line(f"forecast move > {100 * c:.1f}c", [r for r in test if r[key] > c])
    line("forecast move <= 0", [r for r in test if r[key] <= 0])
line("every test moment", test)
