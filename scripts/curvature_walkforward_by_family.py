"""Does the live curve model pick commodity bets as well as crypto ones, and does energy differ at the same readings? (2026-11-09, user: "natgas, oil and Brent are being horrible on commodity_multiscale ... do we need a separate multiscale for energy?")
Question: on the same moments the model lanes could buy (every settled window of the model_buy lanes' series since 11-02, both sides, every 30 s inside each lane's entry window, price and spread bands), fit the live model (curvature readings, curvemodel.fit, trained on crypto AND commodity rows together, as the live refit does) on the EARLIER days only and score each next day: buy the first moment per market side whose predicted net is at or over the training top-15% cut (cut_for) and above zero, held to the result, net per $1 after the fee. Reported: all picks, then by family (crypto; energy = WTI, Brent, natgas; metals = gold, silver, platinum, palladium; copper), each with n, net per $1, win rate, two halves of the test days, and by minutes in (1.5-5, 5-10, 10-14). The family labels are for this report only; the model reads curves, never names. The first three days only train.
Settings: the live lanes' (model_top 0.15, curvature readings, the shared bands), not chosen after the results.
Not covered: the lanes' other gates (volatility ceiling, hot market, spike check), exits, fills.
    scripts/cloud_job.sh scripts/curvature_walkforward_by_family.py
"""
import collections
import os
import time

from kalshi_moonshot import config, curvemodel

os.nice(10)
s = config.reload()
curvemodel.TRAIN_DAYS = (time.time() - 1_793_000_000) / 86400.0 + 0.5  # the tape since 11-02
meta = []
rows = curvemodel.build_rows(s, meta=meta, kind="curvature")
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
print("\nfirst moment per market side, test days only:")
show("all picks", f)
for fam in ("crypto", "energy", "metals", "copper"):
    sub = [i for i in f if family(meta[i][2]) == fam]
    show(fam, sub)
    for lo, hi in ((90, 300), (300, 600), (600, 900)):
        show(f"   {fam}, {lo / 60:.1f}-{hi / 60:.0f} min in", [i for i in sub if lo <= meta[i][1] < hi])
print("DONE")
