"""Does the price's volatility rise as the market runs out of time? (2026-11-18, user: "as time expires the volatility rate increases")

One question on the archived Kalshi quotes of every 15-minute market (data/tape_archive/quotes, 2026-11-09 to 11-16, with the markets' close times from windows.csv): the root-mean-square one-second change of the mid, by the time left, for a market near even (the side's mid 0.20 to 0.80) and for a favourite (0.80 to 0.92 for the leading side). One reading a second (the last quote in each second); only moves between consecutive seconds. Kalshi's quotes only. Nothing fitted: the buckets are minutes of the market.
Run on the Mac: .venv/bin/python scripts/volatility_by_time_left.py > studies/results/<date>-volatility-by-time-left.txt"""
import csv
import glob

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

close = {r[0]: float(r[1]) for r in csv.reader(open("data/tape_archive/windows.csv")) if len(r) >= 3 and r[2] in ("yes", "no")}
EDGES = [(840, 900, "first 1 min"), (600, 840, "10 to 14 min left"), (420, 600, "7 to 10"), (300, 420, "5 to 7"), (180, 300, "3 to 5"), (120, 180, "2 to 3"), (60, 120, "1 to 2"), (30, 60, "30 to 60 s"), (0, 30, "last 30 s")]
acc = {(band, k): [0.0, 0] for band in ("near even", "favourite") for k in range(len(EDGES))}
days = sorted(glob.glob("data/tape_archive/quotes/day=*/part-0.parquet"))
for f in days:
    t = pq.read_table(f, columns=["ts", "ticker", "bid", "ask"])
    t = t.filter(pc.match_substring(t["ticker"], "15M-")).to_pandas().sort_values(["ticker", "ts"])
    for ticker, g in t.groupby("ticker", sort=False):
        if ticker not in close:
            continue
        ok = (g["bid"] > 0) & (g["ask"] > 0) & (g["ask"] <= 1) & (g["ask"] >= g["bid"])
        g = g[ok]
        if len(g) < 30:
            continue
        sec = np.floor(g["ts"].to_numpy()).astype(np.int64)
        mid = pd.Series(((g["bid"] + g["ask"]) / 2.0).to_numpy(), index=sec).groupby(level=0).last()
        s, m = mid.index.to_numpy(), mid.to_numpy()
        d = np.diff(m)
        one = np.diff(s) == 1
        left = close[ticker] - s[1:]
        lead = np.maximum(m[1:], 1.0 - m[1:])
        for k, (lo, hi, _) in enumerate(EDGES):
            inb = one & (left >= lo) & (left < hi)
            for band, sel in (("near even", inb & (lead < 0.80)), ("favourite", inb & (lead >= 0.80) & (lead <= 0.92))):
                if sel.any():
                    a = acc[(band, k)]
                    a[0] += float((d[sel] ** 2).sum())
                    a[1] += int(sel.sum())
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; root-mean-square one-second change of the mid, in cents, by the time left")
print(f"  {'time left':20s} | {'near even (lead under 0.80)':>28s} | {'favourite (lead 0.80 to 0.92)':>30s}")
base = {}
for band in ("near even", "favourite"):
    a = acc[(band, 2)]
    base[band] = (a[0] / a[1]) ** 0.5 if a[1] else float("nan")
for k, (_, _, name) in enumerate(EDGES):
    cells = []
    for band in ("near even", "favourite"):
        a = acc[(band, k)]
        r = (a[0] / a[1]) ** 0.5 if a[1] else float("nan")
        cells.append(f"{100 * r:5.2f}c  x{r / base[band]:4.2f} of 7-10 min  (n {a[1]:>9,d})")
    print(f"  {name:20s} | {cells[0]} | {cells[1]}")
print("DONE")
