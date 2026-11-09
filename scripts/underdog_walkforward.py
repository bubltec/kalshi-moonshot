"""The model on the underdog lane (2026-11-09, user: "implement it live", prices 0.10-0.40, minutes 0-7.5): what would its picks have paid? Walk-forward of the live curvature model fitted on the underdog category's own moments (crypto series, ask 0.10-0.40, first moment to 7.5 minutes, spread cap as the lane): fit on the EARLIER days only, score each next day (the first three days only train): buy the first moment per market side whose predicted net is at or over the training top-15% cut and above zero, held to the result, net per $1 after the fee; reported overall, by price (0.10-0.20, 0.20-0.30, 0.30-0.40), by minutes in and halves, next to every moment in the same band (what buying without the model gives). Settings are the lane's, set before the results.
Not covered: the lane's other gates and exits (spike check, shock exit, value_exit 0.5), fills.
    scripts/cloud_job.sh scripts/underdog_walkforward.py
"""
import collections
import os
import time

from kalshi_moonshot import config, curvemodel

os.nice(10)
s = config.reload()
curvemodel.TRAIN_DAYS = (time.time() - 1_793_000_000) / 86400.0 + 0.5  # the tape since 11-02
meta = []
rows = curvemodel.build_rows(s, meta=meta, kind="curvature", category="underdog")
print("rows", len(rows), "market sides", len({(m[2], m[3]) for m in meta}), flush=True)
N = len(curvemodel.KINDS["curvature"])
ENERGY, METALS = ("KXWTI15M", "KXBRENT15M", "KXNATGAS15M"), ("KXGOLD15M", "KXSILVER15M", "KXPLATINUM15M", "KXPALLADIUM15M")


def family(ticker):
    ser = ticker.split("-")[0]
    return "energy" if ser in ENERGY else "metals" if ser in METALS else "copper" if ser == "KXCOPPER15M" else "crypto"


day = [int(m[0] // 86400) for m in meta]
days = sorted(set(day))
by_day = collections.defaultdict(list)
for i, d in enumerate(day):
    by_day[d].append(i)
picked = []
for di, d in enumerate(days):
    if di < 3:
        continue
    train = [i for dd in days[:di] for i in by_day[dd]]
    model = curvemodel.fit([(rows[i][0][:N], rows[i][1], rows[i][2]) for i in train], kind="curvature")
    if model is None:
        print("day", d, "not fitted", flush=True)
        continue
    cut = curvemodel.cut_for(model, 0.15)
    for i in by_day[d]:
        p = curvemodel.predict(model, rows[i][0][:N], rows[i][1])
        if p >= cut and p > 0:
            picked.append(i)
    print("day", d, "trained on", len(train), "rows, cut", round(cut, 3), flush=True)
mid = sorted(m[0] for m in meta)[len(meta) // 2]


def first(idx):
    seen = {}
    for i in sorted(idx, key=lambda i: meta[i][1]):
        seen.setdefault((meta[i][2], meta[i][3]), i)
    return list(seen.values())


def show(label, idx):
    if not idx:
        print(f"   {label:34s} n=0")
        return
    n = len(idx)
    net = [rows[i][2] for i in idx]
    mean = sum(net) / n
    se = (sum((x - mean) ** 2 for x in net) / max(n - 1, 1)) ** 0.5 / n**0.5
    a = [rows[i][2] for i in idx if meta[i][0] <= mid]
    b = [rows[i][2] for i in idx if meta[i][0] > mid]
    won = sum(1 for x in net if x > 0)
    print(f"   {label:34s} n={n:5d} net per $1 {mean:+.3f} (se {se:.3f}) won {100 * won / n:4.1f}% | halves {sum(a) / max(len(a), 1):+.3f} (n={len(a)}) / {sum(b) / max(len(b), 1):+.3f} (n={len(b)})")


f = first(picked)
print("\nfirst moment per market side, test days only, by the price paid:")
def show_price(label, idx):
    if not idx:
        print(f"   {label:24s} n=0")
        return
    n = len(idx)
    net = [rows[i][2] for i in idx]
    mean = sum(net) / n
    se = (sum((x - mean) ** 2 for x in net) / max(n - 1, 1)) ** 0.5 / n**0.5
    a = [rows[i][2] for i in idx if meta[i][0] <= mid]
    b = [rows[i][2] for i in idx if meta[i][0] > mid]
    won = sum(1 for x in net if x > 0)
    ask = sum(rows[i][1] for i in idx) / n
    print(f"   {label:24s} n={n:5d} avg ask {ask:.3f} won {100 * won / n:4.1f}% net per $1 {mean:+.3f} (se {se:.3f}) | halves {sum(a) / max(len(a), 1):+.3f} (n={len(a)}) / {sum(b) / max(len(b), 1):+.3f} (n={len(b)})")
show_price("all picks", f)
for lo, hi in ((0.10, 0.20), (0.20, 0.30), (0.30, 0.40)):
    show_price(f"ask {lo:.2f}-{hi:.2f}", [i for i in f if lo <= rows[i][1] < hi or (hi == 0.40 and rows[i][1] == 0.40)])
print("\nthe same buckets over ALL moments in the test days (what buying without the model would give):")
test_all = first([i for d in days[3:] for i in by_day[d]])
for lo, hi in ((0.10, 0.20), (0.20, 0.30), (0.30, 0.40)):
    show_price(f"ask {lo:.2f}-{hi:.2f}", [i for i in test_all if lo <= rows[i][1] < hi or (hi == 0.40 and rows[i][1] == 0.40)])
print("DONE")
