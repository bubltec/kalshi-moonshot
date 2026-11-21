"""Does Kalshi's quote lag Kalshi's own spot reference? (2026-11-21, user: "the spot price tends to be slightly ahead of the graph in the predictions market")
Question: across windows, at what lag L (seconds) is the correlation highest between the spot's 5-second change and the yes mid's 5-second change L seconds later? Only quotes between 120 s and 780 s after the open with the yes mid between 0.10 and 0.90 (away from settlement). Overall and by thirds of windows ordered by the spot's typical-minute size (a curve measure, never an asset). Read-only; study cache.
    .venv/bin/python scripts/spot_quote_lag.py [CACHE_DIR]"""
import bisect, math, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import study_cache as sc

cache = sc.Cache(sys.argv[1] if len(sys.argv) > 1 else ".claude/worktrees/study-cache-02a1b7/job-results/20261121-074523-83364")


def second_grid(T, V, a, b):
    j = bisect.bisect_right(T, a) - 1
    cur = V[j] if j >= 0 else None
    out = []
    for s_ in range(a, b):
        while j + 1 < len(T) and T[j + 1] <= s_:
            j += 1
            cur = V[j]
        out.append(cur)
    return out


LAGS = [-10, -5, 0, 1, 2, 3, 5, 8, 12, 20, 30]
STEP = 5
acc = []  # per window: (unit, {lag: (sxy,sxx,syy,n)})
for w in cache.windows:
    if w["result"] not in ("yes", "no"):
        continue
    close = int(w["close"]); a = close - 900
    T, V = cache.ticks(w["index"])
    sp = second_grid(T, V, a, close)
    qt, qb, qa = cache.quotes(w)
    if len(qt) < 200 or None in sp:
        continue
    bg = second_grid(qt, qb, a, close); ag = second_grid(qt, qa, a, close)
    if None in bg[120:] or None in ag[120:]:
        continue
    f0 = bg[120]; g0 = ag[120]
    mid = [((x if x is not None else f0) + (y if y is not None else g0)) / 2 for x, y in zip(bg, ag)]
    ds = [sp[k] - sp[k - STEP] for k in range(STEP, 900)]
    unit = math.sqrt(sum(x * x for x in ds) / len(ds))
    if unit <= 0:
        continue
    stats = {}
    for L in LAGS:
        sxy = sxx = syy = 0.0; n = 0
        for k in range(120, 780):
            if not (0.10 <= mid[k] <= 0.90):
                continue
            k2 = k + L
            if k2 - STEP < 0 or k2 >= 900:
                continue
            x = (sp[k] - sp[k - STEP]) / unit
            y = mid[k2] - mid[k2 - STEP]
            sxy += x * y; sxx += x * x; syy += y * y; n += 1
        stats[L] = (sxy, sxx, syy, n)
    acc.append((unit / max(abs(sp[0]), 1e-9), stats))
print(f"{len(acc)} windows")
acc.sort(key=lambda t: t[0])


def table(rows, label):
    print(label)
    for L in LAGS:
        sxy = sum(r[1][L][0] for r in rows); sxx = sum(r[1][L][1] for r in rows); syy = sum(r[1][L][2] for r in rows)
        print(f"  lag {L:4d}s corr {sxy / math.sqrt(sxx * syy):.4f}")


table(acc, "all windows (spot change at t vs yes-mid change at t+lag)")
third = len(acc) // 3
table(acc[:third], "quietest third by relative spot size")
table(acc[-third:], "most volatile third")
print("DONE")
