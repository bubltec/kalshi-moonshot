"""What separates a good cheap buy from a bad one: the state record, walk-forward (2026-11-08, user: "the issue isn't the price band ... we had plenty of great wins when we bought correctly in the lower price bands").
Question: the lane buys when the ridge's predicted net is in its top 20% and above zero. Among those buys, does a record of states (minutes into the window, the cushion's z for the time left, the price band) learned only from EARLIER days tell the buys that pay from the ones that do not? CLAUDE.md rule 8: buy a state because its own recorded result paid, over enough earlier markets, at the buy's price band; rule 4: halves, standard errors, grid fixed in advance.
Setup: the moments of curvemodel.build_rows (every settled crypto 15-minute window since 11-01, both sides, every 30 s, the model lane's price band 0.20-0.70, spread inside the lane's cap). z = Z13, the bet side's cushion over what the seconds left can move at the 13 minute rate. State cells: minutes into the window in 2 minute buckets (7), z in six bands (under -1.5, -1.5 to -1, -1 to -0.5, -0.5 to 0, 0 to 0.5, over 0.5), price band (under 0.35, 0.35 to 0.50, over 0.50).
Record, per test day, from the days before it only: for each market side and cell the first moment in the cell (the moments of one window overlap), n and the sum of net per $1 after the fee, held to the result. A cell's shrunk score is sum / (n + 30). A moment is skipped when its cell has at least 50 earlier first-moments and a negative score. Variants: time (minutes only), z (z only), time_z, time_z_price.
Baseline: the ridge's live rule on each test day (fit on the earlier days, the training top-20% cut, and above zero), first moment per market side. Each variant: the first non-skipped pick per market side. Reported: n, net per $1 with its standard error, win rate against the price, and each half's net with its standard error; the skipped picks (what the record turned down) the same way; and the cheap picks (ask under 0.35) alone.
Not covered: the 60-second settlement average, queue, sales before the result, live fills.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/cheap_buy_states_study.py
"""

import collections
import math
import os
import time

from kalshi_moonshot import config, curvemodel

os.nice(15)
s = config.reload()
curvemodel.TRAIN_DAYS = (time.time() - 1_793_000_000) / 86400.0 + 0.5
meta = []
rows = curvemodel.build_rows(s, meta=meta, timed=True)
print("rows", len(rows), "market sides", len({(m[2], m[3]) for m in meta}))
day = [int(m[0] // 86400) for m in meta]
days = sorted(set(day))
by_day = collections.defaultdict(list)
for i, d in enumerate(day):
    by_day[d].append(i)
won = [1 if r[2] > 0 else 0 for r in rows]
Z13 = 21
SHRINK, MIN_N = 30.0, 50
mid = sorted(m[0] for m in meta)[len(meta) // 2]


def tb(i):
    return min(6, int(meta[i][1] // 120))


def zb(i):
    z = rows[i][0][Z13]
    return 0 if z < -1.5 else 1 if z < -1.0 else 2 if z < -0.5 else 3 if z < 0.0 else 4 if z < 0.5 else 5


def pb(i):
    a = rows[i][1]
    return 0 if a < 0.35 else 1 if a < 0.50 else 2


KEYS = {"time": lambda i: (tb(i),), "z": lambda i: (zb(i),), "time_z": lambda i: (tb(i), zb(i)), "time_z_price": lambda i: (tb(i), zb(i), pb(i))}
picks = []  # (i, {variant: skipped?})
for di, d in enumerate(days):
    if di < 3:
        continue
    train = [i for dd in days[:di] for i in by_day[dd]]
    sub = train[::3]
    rid = curvemodel.fit([(rows[i][0][:18], rows[i][1], rows[i][2]) for i in sub])
    tr = sorted(curvemodel.predict(rid, rows[i][0][:18], rows[i][1]) for i in train[::5])
    cut = max(tr[int(0.8 * (len(tr) - 1))], 0.0)
    rec = {v: collections.defaultdict(lambda: [0, 0.0]) for v in KEYS}
    seen = set()
    for i in train:
        for v, kf in KEYS.items():
            k = (meta[i][2], meta[i][3], v, kf(i))
            if k in seen:
                continue
            seen.add(k)
            c = rec[v][kf(i)]
            c[0] += 1
            c[1] += rows[i][2]
    for i in by_day[d]:
        if curvemodel.predict(rid, rows[i][0][:18], rows[i][1]) < cut:
            continue
        skip = {}
        for v, kf in KEYS.items():
            n, tot = rec[v].get(kf(i), (0, 0.0))
            skip[v] = n >= MIN_N and tot / (n + SHRINK) < 0.0
        picks.append((i, skip))
print("test days", len(days) - 3, "| ridge picks (every moment):", len(picks))


def first(items):
    seen = {}
    for i in sorted(items, key=lambda i: meta[i][1]):
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
        print(f"   {label:24s} n=0")
        return
    a = [rows[i][2] for i in idx if meta[i][0] <= mid]
    b = [rows[i][2] for i in idx if meta[i][0] > mid]
    print(f"   {label:24s} net per $1 {ms([rows[i][2] for i in idx])} won {100 * sum(won[i] for i in idx) / len(idx):4.1f}% vs price {100 * sum(rows[i][1] for i in idx) / len(idx):4.1f}% | first half {ms(a)} | second half {ms(b)}")


base = first([i for i, _ in picks])
print("\nthe ridge's live rule, first pick per market side:")
show("all picks", base)
show("cheap picks (ask < 0.35)", [i for i in base if rows[i][1] < 0.35])
show("other picks (ask >= 0.35)", [i for i in base if rows[i][1] >= 0.35])
for v in KEYS:
    kept = first([i for i, sk in picks if not sk[v]])
    skipped = first([i for i, sk in picks if sk[v]])
    print(f"\nstate record '{v}': the first non-skipped pick per market side, and the picks it turned down")
    show("kept", kept)
    show("kept, cheap", [i for i in kept if rows[i][1] < 0.35])
    show("kept, other", [i for i in kept if rows[i][1] >= 0.35])
    show("skipped (turned down)", skipped)
print("\ncell table (all test days, ridge picks, first per market side and cell; cells with n >= 100):")
cells = collections.defaultdict(list)
seen = set()
for i, _ in sorted(picks, key=lambda p: meta[p[0]][1]):
    k = (meta[i][2], meta[i][3], tb(i), zb(i))
    if k in seen:
        continue
    seen.add(k)
    cells[(tb(i), zb(i))].append(i)
names = ["z<-1.5", "-1.5..-1", "-1..-.5", "-.5..0", "0..0.5", ">0.5"]
for (t, z), idx in sorted(cells.items()):
    if len(idx) >= 100:
        show(f"{t * 2}-{t * 2 + 2} min, {names[z]}", idx)
print("DONE")
