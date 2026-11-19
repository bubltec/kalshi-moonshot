"""Early favourite buys at the highs. (2026-11-19, user: "early buy in the highs is always risk")

One question on the archived Kalshi quote curves (all crypto series, 2026-11-09 to the last archived day, whole market length, 30 s marks from 2 minutes into the window): a favourite side (ask 0.60 to 0.85, spread 4c or less) bought at the ask and held to settlement (fee 0.07 p (1 - p) on the buy), split by where the side's mid sits in its own recent range: POSITION = (mid now - lowest mid) / (highest - lowest) over the last 5 minutes (or since the open when less than 5 minutes have run, at least 2), taken only when the range is at least 1 typical minute (rms one-second move from before the mark times sqrt(60)); AT THE HIGH = position 0.9 or more, MID = 0.3 to 0.9, AT THE LOW = under 0.3, and FLAT = a range under 1 typical minute. By seconds left (13-10, 10-7, 7-4, 4-0.5 minutes): bets, win rate less ask, net per $100; and the same for the early buckets by day. Counts are marks (overlapping in time, coins of one wave overlap). No coin is named. Settings chosen after seeing the results are said to be so.
Run on the Mac: .venv/bin/python scripts/crypto_early_high_buys.py > studies/results/<date>-crypto-early-high-buys.txt"""
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
            d2 = np.where(np.isnan(a["d2"]), 0.0, a["d2"])
            cs, cn = np.cumsum(d2), np.cumsum((~np.isnan(a["d2"])).astype(float))
            for way in ("yes", "no"):
                sa = a["a"] if way == "yes" else 1.0 - a["b"]
                sb = a["b"] if way == "yes" else 1.0 - a["a"]
                sm = (sa + sb) / 2.0
                okv = np.flatnonzero(~np.isnan(sm))
                if len(okv) < 100:
                    continue
                first = int(okv[0])
                win = a["result"] == way
                for k in range(120, 870, 30):
                    p, bid = sa[k], sb[k]
                    if np.isnan(p) or np.isnan(bid) or not (0.60 <= p < 0.85) or p - bid > 0.04 or cn[k - 1] < 90 or cs[k - 1] <= 0:
                        continue
                    lo_i = max(first, k - 300)
                    if k - lo_i < 60:
                        continue
                    w = sm[lo_i:k + 1]
                    if np.isnan(w).any():
                        continue
                    unit = float(np.sqrt(cs[k - 1] / cn[k - 1]) * math.sqrt(60.0))
                    rng_ = float(w.max() - w.min())
                    state = "flat" if rng_ < unit else ("high" if (w[-1] - w.min()) / rng_ >= 0.9 else ("low" if (w[-1] - w.min()) / rng_ < 0.3 else "mid"))
                    rows.append((day, 900 - k, float(p), win, state))
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {len(rows)} favourite-side marks (ask 0.60-0.85, spread <= 4c), held to settlement")
left = np.array([r[1] for r in rows]); ask = np.array([r[2] for r in rows]); won = np.array([1.0 if r[3] else 0.0 for r in rows]); st = np.array([r[4] for r in rows]); dd = np.array([r[0] for r in rows])
net = (won - ask - 0.07 * ask * (1 - ask)) / ask
buckets = ((780, 601, "13 to 10 min"), (600, 421, "10 to 7 min"), (420, 241, "7 to 4 min"), (240, 30, "4 to 0.5 min"))
print("--- by seconds left and where the side sits in its last 5 minutes: bets | mean ask | win rate | win less ask | net per $100")
for hi, lo, nm in buckets:
    for s_ in ("high", "mid", "low", "flat"):
        m = (left <= hi) & (left >= lo) & (st == s_)
        if m.sum() >= 80:
            print(f"  {nm:13s} at the {s_:5s} | {int(m.sum()):6d} | {ask[m].mean():.3f} | {100 * won[m].mean():5.1f}% | {100 * (won[m].mean() - ask[m].mean()):+5.1f}c | {100 * net[m].mean():+6.1f}")
print("--- early (13 to 7 minutes left) by day: net per $100 at the high / mid / low / flat (bets)")
early = left >= 421
for d in sorted(set(dd)):
    parts = []
    for s_ in ("high", "mid", "low", "flat"):
        m = early & (st == s_) & (dd == d)
        parts.append(f"{100 * net[m].mean():+6.1f} ({int(m.sum())})" if m.sum() >= 30 else "   n/a")
    print(f"  {d} {' | '.join(parts)}")
print("--- early, at the high, by ask band")
for b0, b1 in ((0.60, 0.70), (0.70, 0.78), (0.78, 0.85)):
    for s_ in ("high", "low"):
        m = early & (st == s_) & (ask >= b0) & (ask < b1)
        if m.sum() >= 60:
            print(f"  ask {b0:.2f}-{b1:.2f} at the {s_:4s} | {int(m.sum()):5d} | win less ask {100 * (won[m].mean() - ask[m].mean()):+5.1f}c | net per $100 {100 * net[m].mean():+6.1f}")
