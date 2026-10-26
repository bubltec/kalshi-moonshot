"""Does a model fitted per coin beat one fitted on all five coins pooled? Out of sample, on the saved case memory (behavior_cases). Places nothing.

Moments: the saved 70-92c crypto moments. Target: edge beyond the price (win - ask), fee-free. Features: ask, spread, elapsed minutes, 15- and 60-second ask moves, minutes held above 70c, the index's distance past the strike, the 7-15 minute index slice, and the move before the window opened.
The earlier 60% of windows train, the later 40% test. Pooled: one ridge regression on every coin. Per coin: one ridge regression per coin. For each, the test moments are split in thirds by the predicted edge and the realized edge of the top and bottom thirds is shown.
    docker compose exec -T bot python - < scripts/per_coin_forecast_study.py
"""
import json
import math
import statistics
from collections import defaultdict

from kalshi_moonshot import config
from kalshi_moonshot.store import Store

s = config.reload()
store = Store(s.db_path, s.exclude_versions)
NAMES = ["ask", "spread", "elapsed_min", "rise_30", "rise_90", "held_min", "dist", "slice_7_15", "prior_15"]
rows = []
for r in store.db.execute("SELECT ticker, series, close_ts, features, won FROM behavior_cases WHERE kind='moment' AND side != '-' AND features != '{}'").fetchall():
    f = json.loads(r["features"])
    f["slice_7_15"] = (f.get("slices") or [None] * 3)[2]
    x = [f.get(k) for k in NAMES]
    if any(v is None for v in x) or not f.get("ask"):
        continue
    rows.append({"coin": r["series"], "cl": r["ticker"], "t": r["close_ts"], "x": [float(v) for v in x], "edge": r["won"] - f["ask"]})
rows.sort(key=lambda r: r["t"])
cut = rows[int(len(rows) * 0.6)]["t"]
train, test = [r for r in rows if r["t"] <= cut], [r for r in rows if r["t"] > cut]
print(f"{len(rows):,} moments; train {len(train):,}, test {len(test):,}; windows per coin in the test: " + ", ".join(f"{c} {len({r['cl'] for r in test if r['coin'] == c})}" for c in sorted({r['coin'] for r in rows})))


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


def fit(rs):
    mean = [statistics.mean(r["x"][k] for r in rs) for k in range(len(NAMES))]
    sd = [statistics.pstdev([r["x"][k] for r in rs]) or 1.0 for k in range(len(NAMES))]
    k_ = len(NAMES) + 1
    xtx = [[0.0] * k_ for _ in range(k_)]
    xty = [0.0] * k_
    for r in rs:
        x = [1.0] + [(v - m) / d for v, m, d in zip(r["x"], mean, sd, strict=True)]
        for i in range(k_):
            xty[i] += x[i] * r["edge"]
            for j in range(k_):
                xtx[i][j] += x[i] * x[j]
    for i in range(1, k_):
        xtx[i][i] += 20.0
    beta = solve(xtx, xty)
    return lambda r: beta[0] + sum(b * (v - m) / d for b, v, m, d in zip(beta[1:], r["x"], mean, sd, strict=True))


def se(g):
    per = defaultdict(list)
    for r in g:
        per[r["cl"]].append(r["edge"])
    mm = [sum(v) / len(v) for v in per.values()]
    return 100 * math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm)) if len(mm) > 2 else float("nan")


def corr(rs, key):
    xs, ys = [r[key] for r in rs], [r["edge"] for r in rs]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))


pooled = fit(train)
per_coin = {c: fit([r for r in train if r["coin"] == c]) for c in {r["coin"] for r in train}}
for r in test:
    r["pooled"], r["own"] = pooled(r), per_coin[r["coin"]](r)
print(f"\\nout-of-sample correlation of predicted with realized edge: pooled {corr(test, 'pooled'):+.3f}, per coin {corr(test, 'own'):+.3f}")
for key, name in (("pooled", "pooled"), ("own", "per coin")):
    ranked = sorted(test, key=lambda r: r[key])
    third = len(ranked) // 3
    lo, hi = ranked[:third], ranked[-third:]
    print(f"  {name:9} bottom third {100 * statistics.mean(r['edge'] for r in lo):+5.1f} pts +-{se(lo):.1f}   top third {100 * statistics.mean(r['edge'] for r in hi):+5.1f} pts +-{se(hi):.1f}")
print("\\nper coin, test half: pooled model vs that coin's own model (top third minus bottom third, realized edge in points)")
for c in sorted(per_coin):
    g = [r for r in test if r["coin"] == c]
    row = []
    for key in ("pooled", "own"):
        ranked = sorted(g, key=lambda r: r[key])
        third = len(ranked) // 3
        row.append(100 * (statistics.mean(r["edge"] for r in ranked[-third:]) - statistics.mean(r["edge"] for r in ranked[:third])))
    print(f"  {c:9} pooled {row[0]:+5.1f}   own {row[1]:+5.1f}   ({len({r['cl'] for r in g})} test windows)")
