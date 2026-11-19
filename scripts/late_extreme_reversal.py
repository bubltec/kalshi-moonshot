"""How often does a crypto side at 0.98 or more with about 2 minutes left fall to 0.75 before the close? (2026-11-19, user: "how do you know it won't fill?")
One question on the archived Kalshi quotes (all crypto series, 2026-11-09 to the last archived day): at 100 to 130 s before the close, the first second where either side's bid is 0.98 or more (and the other side's ask is 0.03 or less); then the lowest the leader's bid reaches in the seconds that follow, and how many fell to 0.90, 0.75 and 0.50 or lower, and how many settled against it. Counts are windows and coins; no price filter beyond the stated one.
Run on the Mac: .venv/bin/python scripts/late_extreme_reversal.py > studies/results/<date>-late-extreme-reversal.txt"""
import csv
import glob
from collections import defaultdict

import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq

SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M")
res = {r[0]: (float(r[1]), r[2]) for r in csv.reader(open("data/tape_archive/windows.csv")) if len(r) >= 3 and r[2] in ("yes", "no")}
n = 0
low = []
against = 0
for f in sorted(glob.glob("data/tape_archive/quotes/day=*/part-0.parquet")):
    t = pq.read_table(f, columns=["ts", "ticker", "bid", "ask"])
    t = t.filter(pc.match_substring(t["ticker"], "15M-"))
    df = t.to_pandas()
    df = df[df["ticker"].str.split("-").str[0].isin(SERIES)].sort_values(["ticker", "ts"])
    for ticker, g in df.groupby("ticker", sort=False):
        if ticker not in res:
            continue
        close, result = res[ticker]
        ts, b, a = g["ts"].to_numpy(), g["bid"].to_numpy(), g["ask"].to_numpy()
        sel = (ts >= close - 130) & (ts <= close - 100) & (b > 0) & (a > 0)
        if not sel.any():
            continue
        i0 = np.flatnonzero(sel)[0]
        for way in ("yes", "no"):
            sb = b if way == "yes" else 1.0 - a
            if sb[i0] < 0.98:
                continue
            after = (ts >= ts[i0]) & (ts < close) & (b > 0) & (a > 0)
            lo = float(sb[after].min())
            n += 1
            low.append(lo)
            against += result != way
low = np.array(low)
print(f"{n} windows and coins with a side bid at 0.98 or more about 2 minutes before the close")
for lv in (0.95, 0.90, 0.75, 0.50):
    print(f"  fell to {lv:.2f} or lower before the close: {int((low <= lv).sum())} ({100.0 * (low <= lv).mean():.2f}%)")
print(f"  settled against it: {against} ({100.0 * against / max(n, 1):.2f}%)")
