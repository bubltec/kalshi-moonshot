"""Q2: held to settlement, what does the Crypto Leader favourite and the cheap coins pay?
Windows from crypto_lead_common (n, days in the output). At each 60 s mark, per window: favourite = coin with the highest mid; bought YES at its ask (needs ask < 1), fee 0.07 p (1-p) on the buy, none at settlement; paid 1 if that coin wins. Cheap book: every other coin whose ask <= 0.25, bought YES at the ask the same way. Also the favourite's NO (cost 1 - bid of the favourite). Reported by time left (13-10, 10-7, 7-4, 4-2, 2-0.5 minutes) and ask band, per $100 staked, and by day. Marks of one window overlap, so counts are bets, not independent events; windows is shown too. Bands and buckets fixed before looking (the project's usual ones).
Run: PYTHONPATH=scripts .venv/bin/python scripts/crypto_lead_q2_pricing.py > studies/results/2026-11-19-crypto-lead-q2-pricing.txt"""
from collections import defaultdict

import numpy as np

from crypto_lead_common import fee, load_windows

W = load_windows()
BUCKETS = [(13, 10), (10, 7), (7, 4), (4, 2), (2, 0.5)]
FAV_BANDS = [(0, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 0.9), (0.9, 0.97), (0.97, 1.0001)]
CHEAP_BANDS = [(0, 0.05), (0.05, 0.10), (0.10, 0.18), (0.18, 0.26)]
rows = []  # (kind, day, bucket, band, ask, win, window_ix)
for wi, w in enumerate(W):
    for k in range(60, 900, 60):
        left = (900 - k) / 60
        bk = next((i for i, (hi, lo) in enumerate(BUCKETS) if lo < left <= hi), None)
        if bk is None:
            continue
        b, a = w["bid"][:, k], w["ask"][:, k]
        if np.isnan(a).any():
            continue
        m = (a + b) / 2
        f = int(np.argmax(m))
        if a[f] < 1:
            rows.append(("fav", w["day"], bk, a[f], bool(w["win"][f]), wi))
        if b[f] > 0:
            rows.append(("favNO", w["day"], bk, 1 - b[f], not w["win"][f], wi))
        for i in range(5):
            if i != f and 0 < a[i] <= 0.26:
                rows.append(("cheap", w["day"], bk, a[i], bool(w["win"][i]), wi))


def stat(sel):
    n = len(sel)
    if n == 0:
        return "n=0"
    p = np.array([r[3] for r in sel]); win = np.array([r[4] for r in sel], float)
    cost = p + np.array([fee(x) for x in p])
    net = (win - cost).sum()
    wins_ = len({r[5] for r in sel})
    return f"n={n:5d} win={wins_:3d}wd ask={p.mean():.3f} winrate={win.mean():.3f} gap={win.mean()-p.mean():+.3f} net/$100={100*net/cost.sum():+6.1f}"


print(f"windows n={len(W)}, {W[0]['day']} to {W[-1]['day']}; fee 0.07p(1-p) on the buy; held to settlement")
for kind, bands in (("fav", FAV_BANDS), ("favNO", FAV_BANDS), ("cheap", CHEAP_BANDS)):
    name = {"fav": "FAVOURITE yes at ask", "favNO": "FAVOURITE no at 1-bid", "cheap": "CHEAP coins yes (not the favourite, ask<=0.26)"}[kind]
    print(f"\n=== {name} ===")
    print("all:", stat([r for r in rows if r[0] == kind]))
    print("by time left (minutes):")
    for i, (hi, lo) in enumerate(BUCKETS):
        print(f"  {hi}-{lo}:", stat([r for r in rows if r[0] == kind and r[2] == i]))
    print("by ask band:")
    for lo, hi in bands:
        print(f"  {lo:.2f}-{min(hi,1):.2f}:", stat([r for r in rows if r[0] == kind and lo <= r[3] < hi]))
    print("by time left x ask band (n>=40 shown):")
    for i, (bh, bl) in enumerate(BUCKETS):
        for lo, hi in bands:
            s = [r for r in rows if r[0] == kind and r[2] == i and lo <= r[3] < hi]
            if len(s) >= 40:
                print(f"  {bh}-{bl} min, ask {lo:.2f}-{min(hi,1):.2f}:", stat(s))
    print("by day:")
    for d in sorted({r[1] for r in rows}):
        print(f"  {d}:", stat([r for r in rows if r[0] == kind and r[1] == d]))
