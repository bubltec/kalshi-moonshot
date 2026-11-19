"""Does the whole market rotate? (2026-11-19, user: "this probably rotates, and this rotation is the wave we are trying to identify")

One question on the archived Kalshi quote curves and results (the crypto series, 2026-11-09 to the last archived day): is there a rotation in the market-wide direction, between windows and inside a window? 
BETWEEN windows: for each 15-minute close with 6 or more coins settled, S = the share of the coins that settled YES (0 = all down, 1 = all up), and D = S - 0.5. Over consecutive closes (a gap of more than one window breaks the run): the autocorrelation of D at lags 1 to 8 (15 minutes to 2 hours) with its permutation band (the 2.5th and 97.5th percentile of the same statistic on 500 shuffles of the windows), the number of runs of the same sign of D against the shuffled mean, and the mean run length.
INSIDE a window: H1 = the mean over coins of (YES mid at 7.5 minutes less 0.5) in the coin's typical minutes (rms one-second move of the window's first 7.5 minutes times sqrt(60)), H2 = the mean of (mid at 14 minutes less mid at 7.5 minutes) in the same units; the correlation of H1 with H2 across windows (negative = the second half rotates against the first) and the share of windows where H2 has the opposite sign of H1, by the size of |H1| in thirds (a larger first move: does the second half turn more?).
Counts are windows; the shuffle keeps the shares and breaks the order. Settings chosen after seeing the results are said to be so.
Run on the Mac: .venv/bin/python scripts/crypto_market_rotation.py > studies/results/<date>-crypto-market-rotation.txt"""
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
wrows = []
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
        if len(tk) < 6:
            continue
        h1, h2, yes = [], [], 0
        for ticker, a in tk.items():
            m = a["m"]
            d2 = a["d2"][:450]
            d2 = d2[~np.isnan(d2)]
            if len(d2) < 120 or np.isnan(m[450]) or np.isnan(m[840]):
                continue
            u = float(np.sqrt(np.mean(d2)) * math.sqrt(60.0))
            if u <= 0:
                continue
            h1.append((m[450] - 0.5) / u)
            h2.append((m[840] - m[450]) / u)
            yes += a["result"] == "yes"
        if len(h1) >= 6:
            wrows.append((close, yes / len(h1), float(np.mean(h1)), float(np.mean(h2))))
wrows.sort()
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {len(wrows)} closes with 6+ coins")
rng = np.random.default_rng(7)
closes = np.array([w[0] for w in wrows])
D = np.array([w[1] - 0.5 for w in wrows])
brk = np.flatnonzero(np.diff(closes) > 900.5)
segs = np.split(np.arange(len(D)), brk + 1)


def acf(x_segs, lag):
    xs, ys = [], []
    for sg in x_segs:
        if len(sg) > lag:
            xs.append(sg[:-lag]); ys.append(sg[lag:])
    if not xs:
        return float("nan")
    a_, b_ = np.concatenate(xs), np.concatenate(ys)
    return float(np.corrcoef(D[a_], D[b_])[0, 1])


def runs(sg_list):
    n_runs, tot = 0, 0
    for sg in sg_list:
        s = np.sign(D[sg])
        if len(s) == 0:
            continue
        n_runs += 1 + int(np.sum(s[1:] != s[:-1]))
        tot += len(s)
    return n_runs, tot / max(n_runs, 1)


print("--- BETWEEN windows: autocorrelation of the share of coins that settled YES (less 0.5), with the shuffle band")
perm = []
for _ in range(500):
    idx = rng.permutation(len(D))
    Dp = D[idx]
    row = []
    for lag in range(1, 9):
        xs, ys = [], []
        for sg in segs:
            if len(sg) > lag:
                xs.append(sg[:-lag]); ys.append(sg[lag:])
        a_, b_ = np.concatenate(xs), np.concatenate(ys)
        row.append(np.corrcoef(Dp[a_], Dp[b_])[0, 1])
    perm.append(row)
perm = np.array(perm)
for lag in range(1, 9):
    lo, hi = np.percentile(perm[:, lag - 1], [2.5, 97.5])
    v = acf(segs, lag)
    print(f"  lag {lag} ({15 * lag:3d} min): {v:+.3f}  shuffle band {lo:+.3f} to {hi:+.3f}  {'OUTSIDE' if v < lo or v > hi else 'inside'}")
nr, ml = runs(segs)
sims = []
for _ in range(500):
    Dp = D[rng.permutation(len(D))]
    save = D.copy()
    D[:] = Dp
    sims.append(runs(segs))
    D[:] = save
print(f"  runs of the same sign: {nr}, mean length {ml:.2f} windows | shuffled mean runs {np.mean([s[0] for s in sims]):.1f} ({np.percentile([s[0] for s in sims], 2.5):.0f} to {np.percentile([s[0] for s in sims], 97.5):.0f}), mean length {np.mean([s[1] for s in sims]):.2f}")
print("--- INSIDE a window: the market-wide first half (7.5 min) against the second half, in typical minutes")
H1 = np.array([w[2] for w in wrows]); H2 = np.array([w[3] for w in wrows])
print(f"  corr(first half, second half) {np.corrcoef(H1, H2)[0, 1]:+.3f} over {len(H1)} windows; second half opposite to the first in {100.0 * np.mean(np.sign(H1) != np.sign(H2)):.1f}%")
cuts = np.quantile(np.abs(H1), [1 / 3, 2 / 3])
for nm, lo, hi in (("small first move", -1, cuts[0]), ("middle", cuts[0], cuts[1]), ("large", cuts[1], 99)):
    q = (np.abs(H1) > lo) & (np.abs(H1) <= hi)
    print(f"  {nm:17s} |H1| {max(lo, 0):.2f}-{min(hi, 99):.2f} n {int(q.sum()):3d} | corr {np.corrcoef(H1[q], H2[q])[0, 1]:+.3f} | second half opposite {100.0 * np.mean(np.sign(H1[q]) != np.sign(H2[q])):.1f}% | mean H2 signed with H1 {np.mean(np.sign(H1[q]) * H2[q]):+.2f}")
