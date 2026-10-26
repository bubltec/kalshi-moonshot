"""Do the saved features predict the edge beyond the price on windows they have not seen? Walk-forward on the case memory (behavior_cases). Places nothing.

Windows are put in time order and cut into four blocks. For each of the last three blocks, everything before it is the training set and the block itself is the test.
(1) All features together: least squares of edge (win - ask) on the standardized features; the test block is split into thirds by fitted edge; return after the buy fee per third.
(2) One feature at a time: the training set decides which end of the feature is worse (by its edge) and where the worst quarter starts; in the test block, moments in that worst quarter are skipped.
    The line shows the return of the moments kept against the moments skipped, pooled across the three test blocks, with a standard error across windows.
    docker compose exec -T bot python - < scripts/behavior_walkforward.py
"""
import json
import math
import statistics
from collections import defaultdict

from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
store = Store(s.db_path, s.exclude_versions)
NAMES = ["ask", "spread", "elapsed_min", "rise_30", "rise_90", "dip_90", "held_min", "imbalance", "updates_60", "dist", "prior_15", "move_90", "range_90", "activity_60", "fc_move"] + [f"slice{i}" for i in range(6)]
rows = []
for r in store.db.execute("SELECT ticker, features, won, close_ts FROM behavior_cases WHERE kind='moment' AND side != '-' AND features != '{}' ORDER BY close_ts").fetchall():
    f = json.loads(r["features"])
    for i, z in enumerate(f.get("slices") or []):
        f[f"slice{i}"] = z
    x = [f.get(k) for k in NAMES]
    if any(v is None for v in x) or not f.get("ask"):
        continue
    a = float(f["ask"])
    rows.append({"cl": r["ticker"], "t": r["close_ts"], "x": [float(v) for v in x], "edge": r["won"] - a, "ret": (r["won"] - a - fee(a)) / (a + fee(a)), "won": r["won"]})
print(f"{len(rows):,} moments with every feature, {len({r['cl'] for r in rows})} windows; average return after the buy fee {statistics.mean(r['ret'] for r in rows):+.1%}")
cuts = sorted({r["t"] for r in rows})
edges = [cuts[len(cuts) * k // 4] for k in range(1, 4)] + [cuts[-1] + 1]
blocks = []
lo = -1.0
for hi in edges:
    blocks.append([r for r in rows if lo < r["t"] <= hi] if lo >= 0 else [r for r in rows if r["t"] < hi])
    lo = hi
folds = [(sum(blocks[:k], []), blocks[k]) for k in (1, 2, 3)]


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


def se_pts(g):
    per = defaultdict(list)
    for r in g:
        per[r["cl"]].append(r["ret"])
    mm = [sum(v) / len(v) for v in per.values()]
    return 100 * math.sqrt(sum((x - sum(mm) / len(mm)) ** 2 for x in mm) / (len(mm) - 1) / len(mm)) if len(mm) > 2 else float("nan")


def line(label, g):
    return f"{label:28} n={len(g):5} ({len({r['cl'] for r in g}):3} windows) return {statistics.mean(r['ret'] for r in g):+6.1%} +-{se_pts(g):.1f}" if len(g) > 30 else f"{label:28} only {len(g)}"


print("\\n(1) all features together: test blocks pooled, in thirds of the fitted edge (lowest to highest)")
scored = []
for train, test in folds:
    mean = [statistics.mean(r["x"][k] for r in train) for k in range(len(NAMES))]
    sd = [statistics.pstdev([r["x"][k] for r in train]) or 1.0 for k in range(len(NAMES))]
    k_ = len(NAMES) + 1
    xtx = [[0.0] * k_ for _ in range(k_)]
    xty = [0.0] * k_
    for r in train:
        x = [1.0] + [(v - m) / d for v, m, d in zip(r["x"], mean, sd, strict=True)]
        for i in range(k_):
            xty[i] += x[i] * r["edge"]
            for j in range(k_):
                xtx[i][j] += x[i] * x[j]
    for i in range(1, k_):
        xtx[i][i] += 1.0  # a little ridge: with a few hundred windows the features are tangled together
    beta = solve(xtx, xty)
    for r in test:
        r["fit"] = beta[0] + sum(b * (v - m) / d for b, v, m, d in zip(beta[1:], r["x"], mean, sd, strict=True))
        scored.append(r)
scored.sort(key=lambda r: r["fit"])
third = len(scored) // 3
for n, part in enumerate((scored[:third], scored[third:2 * third], scored[2 * third:])):
    print("  " + line(f"third {n + 1} (fitted {100 * statistics.mean(r['fit'] for r in part):+5.1f} pts)", part))
print("  " + line("all test moments", scored))

print("\\n(2) one feature at a time: skip the worst quarter (chosen on the training blocks), compare kept with skipped")
results = []
for k, name in enumerate(NAMES):
    kept, skipped = [], []
    for train, test in folds:
        pairs = sorted(((r["x"][k], r["edge"]) for r in train))
        q = len(pairs) // 4
        low, high = statistics.mean(e for _, e in pairs[:q]), statistics.mean(e for _, e in pairs[-q:])
        if low < high:
            cutoff = pairs[q][0]
            bad = lambda r, c=cutoff: r["x"][k] <= c  # noqa: E731
        else:
            cutoff = pairs[-q][0]
            bad = lambda r, c=cutoff: r["x"][k] >= c  # noqa: E731
        for r in test:
            (skipped if bad(r) else kept).append(r)
    if len(skipped) > 50 and len(kept) > 50:
        results.append((statistics.mean(r["ret"] for r in kept) - statistics.mean(r["ret"] for r in skipped), name, kept, skipped))
for d, name, kept, skipped in sorted(results, key=lambda x: -x[0]):
    print(f"  {name:12} kept {statistics.mean(r['ret'] for r in kept):+6.1%} (n={len(kept)}) vs skipped {statistics.mean(r['ret'] for r in skipped):+6.1%} (n={len(skipped)})  difference {100 * d:+5.1f} pts")
