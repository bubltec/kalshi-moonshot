"""Are we buying too early? Favourite sides held to settlement by time left and price. (2026-11-19, user: "if we're losing high probability buys, are we buying too early?")

One question on the archived Kalshi quotes (all crypto series, 2026-11-09 to the last archived day, whole market length): at every 60 s mark, each coin's side whose ask is in 0.60 to 0.85 with a spread of 4c or less, bought at the ask and held to settlement with the fee 0.07 p (1 - p) on the buy (none on settlement). By seconds left (13 to 10 minutes, 10 to 7, 7 to 4, 4 to 2, 2 to 0.5) and by ask band (0.60-0.70, 0.70-0.78, 0.78-0.85): bets, mean ask, actual win rate, net per $100 staked. The win rate minus the ask is the market's own mispricing before the fee; the fee is about 1.2 to 1.8c a contract. Marks of one market and coins of one wave overlap; counts are bets, not independent events. Also the same for the lane's own settled paper bets since the 11-18 push (the host decisions, read separately, shown in the chat), not in this file. No coin is named. Settings chosen after seeing the results are said to be so.
Run on the Mac: .venv/bin/python scripts/crypto_buy_time_left.py > studies/results/<date>-crypto-buy-time-left.txt"""
import csv
import glob
import math
from collections import defaultdict

import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq

LO, HI = 0.60, 0.85
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M")
MARKS = list(range(780, 230, -60))
OFFS = list(range(-300, 301, 60))
res = {r[0]: (float(r[1]), r[2]) for r in csv.reader(open("data/tape_archive/windows.csv")) if len(r) >= 3 and r[2] in ("yes", "no")}
events = []
rows = []
days = sorted(glob.glob("data/tape_archive/quotes/day=*/part-0.parquet"))
for f in days:
    day = f.split("day=")[1][:10]
    t = pq.read_table(f, columns=["ts", "ticker", "bid", "ask"])
    t = t.filter(pc.match_substring(t["ticker"], "15M-"))
    df = t.to_pandas()
    df = df[df["ticker"].str.split("-").str[0].isin(SERIES)].sort_values(["ticker", "ts"])
    wins = defaultdict(dict)  # close -> ticker -> arrays per second (mid, bid, ask, d2 prefix)
    for ticker, g in df.groupby("ticker", sort=False):
        if ticker not in res or len(g) < 120:
            continue
        close, result = res[ticker]
        ts = g["ts"].to_numpy()
        yb, ya = g["bid"].to_numpy(), g["ask"].to_numpy()
        ok = (yb > 0) & (ya > 0) & (ya <= 1) & (ya >= yb) & (ts < close)
        ts, yb, ya = ts[ok], yb[ok], ya[ok]
        if len(ts) < 120 or ts[0] > close - 700:
            continue
        mid = (yb + ya) / 2.0
        secs = np.arange(close - 900, close)
        at = np.searchsorted(ts, secs, side="right") - 1
        have = at >= 0
        msec = np.where(have, mid[np.maximum(at, 0)], np.nan)
        asec = np.where(have, ya[np.maximum(at, 0)], np.nan)
        bsec = np.where(have, yb[np.maximum(at, 0)], np.nan)
        d = np.where(have[1:] & have[:-1], np.diff(msec), np.nan)
        wins[close][ticker] = {"m": msec, "a": asec, "b": bsec, "d2": d * d, "result": result}
    for close, tk in wins.items():
        for ticker, a in tk.items():
            sa_y, sb_y = a["a"], a["b"]
            for way in ("yes", "no"):
                sa = sa_y if way == "yes" else 1.0 - sb_y
                sb = sb_y if way == "yes" else 1.0 - sa_y
                win = a["result"] == way
                for k in range(60, 870, 60):
                    p, bid = sa[k], sb[k]
                    if np.isnan(p) or np.isnan(bid) or not (0.60 <= p < 0.85) or p - bid > 0.04:
                        continue
                    rows.append((day, 900 - k, float(p), win))
n = len(rows)
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {n} favourite-side marks (ask 0.60-0.85, spread <= 4c), held to settlement")
R = np.array([(r[1], r[2], 1.0 if r[3] else 0.0) for r in rows])
left, ask, won = R[:, 0], R[:, 1], R[:, 2]
fee = 0.07 * ask * (1 - ask)
net = (won - ask - fee) / ask
buckets = ((780, 601, "13 to 10 min"), (600, 421, "10 to 7 min"), (420, 241, "7 to 4 min"), (240, 121, "4 to 2 min"), (120, 30, "2 to 0.5 min"))
bands = ((0.60, 0.70), (0.70, 0.78), (0.78, 0.85))
print("--- by seconds left and ask band: bets | mean ask | win rate | win rate less ask | net per $100 (fee on the buy)")
for hi, lo, nm in buckets:
    for b0, b1 in bands:
        m = (left <= hi) & (left >= lo) & (ask >= b0) & (ask < b1)
        if m.sum() >= 50:
            print(f"  {nm:13s} ask {b0:.2f}-{b1:.2f} | {int(m.sum()):6d} | {ask[m].mean():.3f} | {100 * won[m].mean():5.1f}% | {100 * (won[m].mean() - ask[m].mean()):+5.1f}c | {100 * net[m].mean():+6.1f}")
    m = (left <= hi) & (left >= lo)
    print(f"  {nm:13s} all bands | {int(m.sum()):6d} | {ask[m].mean():.3f} | {100 * won[m].mean():5.1f}% | {100 * (won[m].mean() - ask[m].mean()):+5.1f}c | {100 * net[m].mean():+6.1f}")
print("--- by day, all, net per $100: " + " ".join(f"{d[5:]} {100 * net[np.array([r[0] == d for r in rows])].mean():+.1f}" for d in sorted(set(r[0] for r in rows))))
