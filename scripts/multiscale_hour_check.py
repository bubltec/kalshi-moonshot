"""The hour view read from 15, 30, 60, 120 and 180 minutes together (2026-11-15, user: "the hour view should look further, but we need the context of the current hour; maybe half hour as well; how do we look at the past 3 hours in 15, 30, 60, 120, 180 mins")

One question, same samples as scripts/decay_window_check.py (every 15-minute window of the last DAYS days for 11 coins, read at 600, 450, 300 and 180 seconds left; the YES chance from the perp's distance to its window-open reference over rate x sqrt(time left); the outcome the perp's direction over the window). The movement rate is built from block rates at the five scales r15, r30, r60, r120, r180 (the root-mean-square one-minute move over that many minutes, each scale twice the one before from 30 on). Variants: each block alone; the geometric mean of all five (each scale a factor of the next, the log-mean); the geometric mean of 60, 120 and 180 (the current hour with the longer context); sqrt(r60 x r180); the mean of the five variances; and WEIGHTS fitted on the earlier two thirds of the windows (a search over weights summing to 1 on the five variances, in steps of 0.1, by log loss) and scored on the last third that the fit never saw, against the live 60-minute block and the best single block on that same last third. Each variant: Brier, log loss and the paired log-loss change against the live reading with its standard error. Not split by curve shape. A check of the chance's quality, not a rule.

Run on the host: python scripts/multiscale_hour_check.py (DAYS=3 to change the look back).
"""
import bisect
import itertools
import math
import os
import time
from statistics import NormalDist

from kalshi_moonshot import config, db
from kalshi_moonshot.watcher import perp_index

SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M")
DAYS = float(os.environ.get("DAYS", "3"))
LEFTS = (600.0, 450.0, 300.0, 180.0)
SCALES = (15, 30, 60, 120, 180)
N = NormalDist()
s = config.load()
tape = db.connect(s.db_path.parent / "x", "tape", shared=True) if False else db.connect(s.tape_path, "tape", shared=True)
now = time.time()
rows_all = []  # (close, ser, left, cushion, paid, [variance per sqrt-second squared at each scale])
for ser in SERIES:
    kp = perp_index(ser)
    if not kp:
        continue
    rows = tape.execute("SELECT DISTINCT ON (FLOOR(ts / 60)) ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY FLOOR(ts / 60), ts", (kp, now - DAYS * 86400 - 3 * 3600 - 3600)).fetchall()
    ts = [float(r[0]) for r in rows]
    lv = [math.log(float(r[1])) for r in rows]

    def level_at(t):
        i = bisect.bisect_right(ts, t) - 1
        return lv[i] if i >= 0 and t - ts[i] <= 120.0 else None
    first = int((now - DAYS * 86400) // 900 * 900 + 900)
    for close in range(first, int(now - 60) // 900 * 900 + 1, 900):
        ref, end = level_at(close - 900), level_at(close)
        if ref is None or end is None or ref == end:
            continue
        paid = 1 if end > ref else 0
        for left in LEFTS:
            t = close - left
            cur = level_at(t)
            marks = [level_at(t - 60.0 * k) for k in range(0, 182)]
            if cur is None or any(m is None for m in marks):
                continue
            m2 = [(marks[k] - marks[k + 1]) ** 2 for k in range(181)]
            var = [sum(m2[:m]) / m / 60.0 for m in SCALES]
            if min(var) <= 0:
                continue
            rows_all.append((close, ser, left, cur - ref, paid, var))
rows_all.sort(key=lambda r: r[0])
print(f"samples {len(rows_all)} from windows closing {time.strftime('%m-%d %H:%M', time.localtime(rows_all[0][0]))} to {time.strftime('%m-%d %H:%M', time.localtime(rows_all[-1][0]))}")


def chance(cush, left, v):
    return min(max(N.cdf(cush / math.sqrt(v * left)), 1e-4), 1 - 1e-4)


def ll(sample, v):
    p = chance(sample[3], sample[2], v)
    return -math.log(p if sample[4] else 1 - p)


def brier(sample, v):
    return (chance(sample[3], sample[2], v) - sample[4]) ** 2


def geo(var, idx):
    return math.exp(sum(math.log(var[i]) for i in idx) / len(idx))


named = {}
for i, m in enumerate(SCALES):
    named[f"block {m}" + (" (live)" if m == 60 else "")] = lambda var, i=i: var[i]
named["geometric mean of all five"] = lambda var: geo(var, range(5))
named["geometric mean of 60, 120, 180"] = lambda var: geo(var, (2, 3, 4))
named["sqrt(r60 x r180)"] = lambda var: math.sqrt(var[2] * var[4])
named["mean of the five variances"] = lambda var: sum(var) / 5.0
base = named["block 60 (live)"]
n = len(rows_all)
print("\nvariant | Brier | log loss | log-loss change vs live (mean +- se; negative = better), all samples")
for name, f in named.items():
    l = [ll(r, f(r[5])) for r in rows_all]
    lb = [ll(r, base(r[5])) for r in rows_all]
    d = [a - b for a, b in zip(l, lb)]
    md = sum(d) / n
    se = math.sqrt(sum((a - md) ** 2 for a in d) / (n - 1) / n)
    print(f"  {name:32s}| {sum(brier(r, f(r[5])) for r in rows_all) / n:.4f} | {sum(l) / n:.4f} | {md:+.4f} +- {se:.4f}")
cut = int(n * 2 / 3)
train, test = rows_all[:cut], rows_all[cut:]
best_w, best = None, 9e9
grid = [k / 10 for k in range(11)]
for w in itertools.product(grid, repeat=5):
    if abs(sum(w) - 1.0) > 1e-9:
        continue
    tot = sum(ll(r, sum(a * b for a, b in zip(w, r[5]))) for r in train[::3]) / len(train[::3])
    if tot < best:
        best, best_w = tot, w
print(f"\nweights fitted on the first {len(train)} samples (variance weights at 15/30/60/120/180): {best_w}; train log loss {best:.4f}")
print(f"scored on the last {len(test)} samples the fit never saw: log loss | change vs live (mean +- se)")
lt = {"fitted weights": [ll(r, sum(a * b for a, b in zip(best_w, r[5]))) for r in test]}
for name in ("block 60 (live)", "block 120", "block 180", "geometric mean of all five", "geometric mean of 60, 120, 180"):
    lt[name] = [ll(r, named[name](r[5])) for r in test]
for name, l in lt.items():
    d = [a - b for a, b in zip(l, lt["block 60 (live)"])]
    md = sum(d) / len(test)
    se = math.sqrt(sum((a - md) ** 2 for a in d) / (len(test) - 1) / len(test))
    print(f"  {name:32s}| {sum(l) / len(test):.4f} | {md:+.4f} +- {se:.4f}")
print("DONE")
