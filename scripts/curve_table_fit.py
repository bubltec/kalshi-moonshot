"""Build data/curve_table.json: the per-coin, per-minute shift in log-odds of each of the 36 move classes (curve.py), learned from the most recent months of 1-minute data. Read-only analysis of downloaded files.

    python scripts/curve_table_fit.py OUT.json MONTHS KLINE_DIR [KLINE_DIR ...]     (needs numpy and pyarrow; MONTHS is how many of the latest months to learn from, 3 by default is plenty)

Uses the same definitions as curve.py: the closes of the first m minutes since the window opened, the class from them, the baseline Phi(z) with the previous 240 minutes' volatility, and a per-class log-odds shift
solved by Newton steps and pulled toward zero by n / (n + 300). A class with fewer than 100 windows is left out (the live code then uses the baseline). A coin's classes are never pooled with another coin's.
"""
import glob
import json
import math
import sys
import time

import numpy as np
import pyarrow.parquet as pq

OUT, MONTHS = sys.argv[1], int(sys.argv[2])
DIRS = sys.argv[3:]
COINS = {"BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M", "XRP": "KXXRP15M", "DOGE": "KXDOGE15M"}


def load(coin):
    files = []
    for d in DIRS:
        files += glob.glob(f"{d}/{coin}USDT_*.parquet") + glob.glob(f"{d}/{coin}USDT/*.parquet")
    t = [pq.read_table(f, columns=["open_time", "open", "close"]) for f in sorted(files)]
    ot = np.concatenate([x["open_time"].to_numpy() for x in t]).astype(np.int64)
    op = np.concatenate([x["open"].to_numpy() for x in t]).astype(float)
    cl = np.concatenate([x["close"].to_numpy() for x in t]).astype(float)
    order = np.argsort(ot)
    ot, op, cl = ot[order], op[order], cl[order]
    keep = np.concatenate([[True], np.diff(ot) > 0])
    return ot[keep], op[keep], cl[keep]


def phi(z):
    return 0.5 * (1.0 + np.vectorize(math.erf)(z / math.sqrt(2.0)))


out = {"made_at": time.time(), "months": MONTHS, "coins": {}, "made_from": ""}
for coin, series in COINS.items():
    try:
        ot, op, cl = load(coin)
    except ValueError:
        print(f"{coin}: no files")
        continue
    last = ot[-1]
    cutoff = last - MONTHS * 30 * 86400 * 1000
    r1 = np.diff(np.log(cl), prepend=np.log(cl[0]))
    n = len(cl)
    csum, csq = np.cumsum(r1), np.cumsum(r1 * r1)

    def vol(i, w):
        a, b = i - w, i
        s1, s2 = csum[b - 1] - csum[a - 1], csq[b - 1] - csq[a - 1]
        return np.sqrt(np.maximum(s2 / w - (s1 / w) ** 2, 1e-14))

    starts = np.where((ot // 60000) % 15 == 0)[0]
    starts = starts[(starts + 15 < n) & (starts > 1500)]
    starts = starts[(ot[starts + 14] - ot[starts]) == 14 * 60000]
    starts = starts[ot[starts] >= cutoff]
    up = (cl[starts + 14] > op[starts]).astype(float)
    out["coins"][series] = {}
    out["made_from"] += f"{coin}: {np.datetime64(int(ot[starts[0]]), 'ms')} to {np.datetime64(int(ot[starts[-1]]), 'ms')} ({len(starts):,} windows); "
    for m in range(5, 13):
        t = starts + m
        v1 = vol(t, 240)
        so_far = np.log(cl[t - 1] / op[starts])
        pb = np.clip(phi(so_far / (v1 * np.sqrt(15 - m))), 1e-4, 1 - 1e-4)
        path = cl[starts[:, None] + np.arange(m)[None, :]]
        hi_, lo_ = path.max(axis=1), path.min(axis=1)
        pos = (path[:, -1] - lo_) / np.maximum(hi_ - lo_, 1e-12)
        since_hi = m - 1 - path.argmax(axis=1)
        pace = np.log(cl[t - 1] / cl[t - 4])
        cls = (so_far > 0).astype(int) * 18 + np.minimum((pos * 3).astype(int), 2) * 6 + np.where(since_hi == 0, 0, np.where(since_hi <= 2, 1, 2)) * 2 + (pace > 0).astype(int)
        lg = np.log(pb / (1 - pb))
        entries = {}
        for c in range(36):
            sel = cls == c
            cnt = int(sel.sum())
            if cnt < 100:
                continue
            d = 0.0
            for _ in range(20):
                pp = 1 / (1 + np.exp(-(lg[sel] + d)))
                d += (up[sel] - pp).sum() / max((pp * (1 - pp)).sum(), 1e-9)
            entries[str(c)] = {"delta": round(float(d * cnt / (cnt + 300)), 4), "n": cnt}
        out["coins"][series][str(m)] = entries
    print(f"{coin}: {len(starts):,} windows from the last {MONTHS} months; classes per minute: " + ", ".join(str(len(v)) for v in out["coins"][series].values()))
with open(OUT, "w") as f:
    json.dump(out, f)
print("wrote", OUT)
