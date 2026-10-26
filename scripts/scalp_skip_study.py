"""Can the saved features tell which scalp attempts to skip? Walk-forward on scalp_cases. Places nothing.

An attempt: a resting bid at the bid of a 30-48c side, then a resting sell 3c above (scalp.py). Its return is ret_taker if the bid filled (the unsold remainder sold at the bid, taker fee) and 0 if it never filled.
Windows are put in time order and cut into four blocks; for each of the last three, everything before it trains and the block tests. Two models, each predicting the return of an attempt from its features:
  ridge   least squares on the standardized features with a little ridge
  past    the average return of the K most similar past attempts (distance over the standardized features)
Then: take the attempts predicted above a threshold, skip the rest. Pooled over the three test blocks, with standard errors across windows. Ordinary taking-everything is the comparison.
    docker compose exec -T bot python - < scripts/scalp_skip_study.py
"""
import heapq
import json
import math
import statistics
from collections import defaultdict

from kalshi_moonshot import config
from kalshi_moonshot.store import Store

s = config.reload()
store = Store(s.db_path, s.exclude_versions)
FEATS = ["ask", "bid", "spread", "elapsed_min", "imbalance", "rise_30", "rise_90", "dip_90", "updates_60", "move_90", "range_90", "activity_60"]
KIND = {"KXBTC15M": 0, "KXETH15M": 0, "KXSOL15M": 0, "KXXRP15M": 0, "KXDOGE15M": 0, "KXEURUSD15M": 1, "KXGBPUSD15M": 1, "KXUSDJPY15M": 1, "KXAUDUSD15M": 1, "KXUSDCAD15M": 1}
rows = []
for r in store.db.execute("SELECT ticker, series, close_ts, features, filled, ret_taker FROM scalp_cases WHERE side != '-' AND features != '{}'").fetchall():
    f = json.loads(r["features"])
    x = [f.get(k) for k in FEATS]
    if any(v is None for v in x):
        continue
    kind = KIND.get(r["series"], 2)
    rows.append({"cl": r["ticker"], "t": r["close_ts"], "x": [float(v) for v in x] + [float(kind == 0), float(kind == 1)], "y": (r["ret_taker"] if r["filled"] else 0.0) or 0.0, "filled": r["filled"]})
rows.sort(key=lambda r: r["t"])
NAMES = FEATS + ["crypto", "fx"]
print(f"{len(rows):,} attempts in {len({r['cl'] for r in rows})} windows; bought {statistics.mean(r['filled'] for r in rows):.0%}; average return per attempt {statistics.mean(r['y'] for r in rows):+.1%}")
cut = [rows[len(rows) * k // 4]["t"] for k in range(1, 4)]
blocks = [[r for r in rows if r["t"] < cut[0]], [r for r in rows if cut[0] <= r["t"] < cut[1]], [r for r in rows if cut[1] <= r["t"] < cut[2]], [r for r in rows if r["t"] >= cut[2]]]
K = 100


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


def se(g):
    per = defaultdict(list)
    for r in g:
        per[r["cl"]].append(r["y"])
    mm = [sum(v) / len(v) for v in per.values()]
    return 100 * math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm)) if len(mm) > 2 else float("nan")


def line(label, g):
    if len(g) < 100:
        return f"  {label:34} only {len(g)}"
    return f"  {label:34} attempts {len(g):6} ({len({r['cl'] for r in g}):4} windows)  bought {statistics.mean(r['filled'] for r in g):4.0%}  return per attempt {statistics.mean(r['y'] for r in g):+6.1%} +-{se(g):.1f}"


test_all = []
for k in (1, 2, 3):
    train, test = sum(blocks[:k], []), blocks[k]
    mean = [statistics.mean(r["x"][j] for r in train) for j in range(len(NAMES))]
    sd = [statistics.pstdev([r["x"][j] for r in train]) or 1.0 for j in range(len(NAMES))]
    z = lambda r: [(v - m) / d for v, m, d in zip(r["x"], mean, sd, strict=True)]  # noqa: E731
    n_ = len(NAMES) + 1
    xtx = [[0.0] * n_ for _ in range(n_)]
    xty = [0.0] * n_
    zt = []
    for r in train:
        x = [1.0] + z(r)
        zt.append(x[1:])
        for i in range(n_):
            xty[i] += x[i] * r["y"]
            for j in range(n_):
                xtx[i][j] += x[i] * x[j]
    for i in range(1, n_):
        xtx[i][i] += 5.0
    beta = solve(xtx, xty)
    memory = list(zip(zt[::4], [r["y"] for r in train][::4], strict=True))
    for r in test[::2]:
        zz = z(r)
        r["ridge"] = beta[0] + sum(b * v for b, v in zip(beta[1:], zz, strict=True))
        near = heapq.nsmallest(K, memory, key=lambda m: sum((a - b) ** 2 for a, b in zip(m[0], zz, strict=True)))
        r["past"] = statistics.mean(m[1] for m in near)
        test_all.append(r)
print(f"\\ntested on {len(test_all):,} attempts from the last three blocks")
print(line("take every attempt", test_all))
for model in ("ridge", "past"):
    ranked = sorted(test_all, key=lambda r: r[model])
    print(f"\\n{model}: attempts in fifths of the predicted return (lowest to highest)")
    n5 = len(ranked) // 5
    for q in range(5):
        part = ranked[q * n5:(q + 1) * n5] if q < 4 else ranked[4 * n5:]
        print(line(f"fifth {q + 1} (predicted {100 * statistics.mean(r[model] for r in part):+5.1f}%)", part))
    for thr in (0.0, 0.02):
        print(line(f"take if predicted > {100 * thr:.0f}%", [r for r in test_all if r[model] > thr]))
        print(line("   skip the rest", [r for r in test_all if r[model] <= thr]))
