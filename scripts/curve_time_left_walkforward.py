"""Does reading the time left make the curve model pick better? (2026-11-08, user: "you absolutely need to build timeleft into the curve prediction").

Question: on the same moments (every settled crypto 15-minute window in the trailing days, both sides, every 30 s, the model lane's price and spread bands), fit the 18-reading model and the 18 + 13 time-left reading model on the earlier days only and score each on the next day. The pick is the live rule: predicted net at or over the training top-share cut and above zero. Net per $1 staked after the full taker fee, held to the result.
Reported, for each model at top 5%, 10%, 20%: every moment and the first moment per market side (as the bot buys): n, net per $1, win rate against the ask, two halves of the test days, and by minutes into the window (0-4, 4-8, 8-15) so the late buys can be read. The first three days only train.
Settings: the shared curvemodel constants (RIDGE, TRAIN_DAYS widened to cover the tape since 11-01). Cutoffs are the grid in the model, not chosen after the results.
Not covered: queue position, sales before the result, the resting-fill fee saving.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/curve_time_left_walkforward.py
"""

import collections
import os
import time

from kalshi_moonshot import config, curvemodel

os.nice(15)
s = config.reload()
curvemodel.TRAIN_DAYS = (time.time() - 1_793_000_000) / 86400.0 + 0.5
meta = []
rows = curvemodel.build_rows(s, meta=meta, timed=True)
print("rows", len(rows), "market sides", len({(m[2], m[3]) for m in meta}))
N0 = 18
day = [int(m[0] // 86400) for m in meta]
days = sorted(set(day))
by_day = collections.defaultdict(list)
for i, d in enumerate(day):
    by_day[d].append(i)


def predict_all(variant, train_idx, test_idx):
    def sub(i):
        return (rows[i][0][:N0] if variant == "old" else rows[i][0], rows[i][1], rows[i][2])

    model = curvemodel.fit([sub(i) for i in train_idx])
    out = {}
    for i in test_idx:
        r = sub(i)
        out[i] = curvemodel.predict(model, r[0], r[1])
    return model, out


res = {v: {q: [] for q in ("0.05", "0.1", "0.2")} for v in ("old", "new")}
for di, d in enumerate(days):
    if di < 3:
        continue
    train = [i for dd in days[:di] for i in by_day[dd]]
    test = by_day[d]
    for v in ("old", "new"):
        model, pr = predict_all(v, train, test)
        for q in res[v]:
            cut = model["cuts"][q]
            res[v][q] += [i for i in test if pr[i] >= cut and pr[i] > 0]
print("test days", len(days) - 3)
mid = sorted(m[0] for m in meta)[len(meta) // 2]


def show(label, idx):
    if not idx:
        print(f"   {label:30s} n=0")
        return
    n = len(idx)
    net = sum(rows[i][2] for i in idx) / n
    won = sum(1 for i in idx if rows[i][2] > 0) / n
    a = [rows[i][2] for i in idx if meta[i][0] <= mid]
    b = [rows[i][2] for i in idx if meta[i][0] > mid]
    print(f"   {label:30s} n={n:5d} net per $1 {net:+.3f} won {100 * won:4.1f}% | halves {sum(a) / max(len(a), 1):+.3f} (n={len(a)}) / {sum(b) / max(len(b), 1):+.3f} (n={len(b)})")


def first(idx):
    seen = {}
    for i in sorted(idx, key=lambda i: meta[i][1]):
        seen.setdefault((meta[i][2], meta[i][3]), i)
    return list(seen.values())


for q in ("0.05", "0.1", "0.2"):
    print(f"\ntop {float(q):.0%}")
    for v in ("old", "new"):
        pick = res[v][q]
        show(f"{v}, every moment", pick)
        show(f"{v}, first per market side", first(pick))
        f = first(pick)
        for lo, hi in ((0, 240), (240, 480), (480, 900)):
            show(f"{v}, first, {lo // 60}-{hi // 60} min in", [i for i in f if lo <= meta[i][1] < hi])
print("DONE")
