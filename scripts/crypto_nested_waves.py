"""Waves within waves: slow market-wide waves and the 15-minute ones. (2026-11-19, user: "it is more likely waves within waves ... a larger wave on the hourly, quarter day, daily, and then short term waves in the 15 minute")

One question on the market-wide direction of the crypto series (D = the share of coins that settled YES at each 15-minute close less 0.5, 2026-11-09 to the last archived day, runs of consecutive closes only): is there structure at the slow scales the user named (hourly = 4 windows, quarter-day = 24, daily = 96), and does the short 15-minute alternation depend on where the slow wave is?
1. Autocorrelation of D at lags 1 to 120 windows (15 min to 30 h), reporting the lags outside a permutation band (2.5th to 97.5th percentile of 300 shuffles of the windows, per lag) and the longest run of consecutive closes available (a daily cycle needs 96+).
2. Slow state = the mean of D over the previous 4, 24 windows (hour, quarter-day). The next window's D by slow state (up, down) and by the last window's D (up, down): mean next D, the share of next windows that went up, windows per cell. If the short alternation is a wave inside a larger one, the last-window effect should differ by slow state.
3. The same shuffle test for each cell difference (the effect of the last window within the up slow state less within the down slow state; 300 shuffles).
Counts are windows, which are not independent; 8 days hold about 8 daily cycles, too few to say anything about a daily wave. Settings chosen after seeing the results are said to be so.
Run on the Mac: .venv/bin/python scripts/crypto_nested_waves.py > studies/results/<date>-crypto-nested-waves.txt"""
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
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {len(wrows)} closes")
wrows.sort()
closes = np.array([w[0] for w in wrows])
D = np.array([w[1] - 0.5 for w in wrows])
brk = np.flatnonzero(np.diff(closes) > 900.5)
segs = np.split(np.arange(len(D)), brk + 1)
print(f"runs of consecutive closes: {len(segs)}; lengths {sorted([len(s) for s in segs], reverse=True)[:8]}")
rng = np.random.default_rng(11)


def acf_all(Dv, lags):
    out = []
    for lag in lags:
        xs, ys = [], []
        for sg in segs:
            if len(sg) > lag:
                xs.append(sg[:-lag]); ys.append(sg[lag:])
        if not xs:
            out.append(np.nan); continue
        a_, b_ = np.concatenate(xs), np.concatenate(ys)
        out.append(np.corrcoef(Dv[a_], Dv[b_])[0, 1])
    return np.array(out)


LAGS = list(range(1, 121))
obs = acf_all(D, LAGS)
perm = np.array([acf_all(D[rng.permutation(len(D))], LAGS) for _ in range(300)])
lo, hi = np.nanpercentile(perm, 2.5, axis=0), np.nanpercentile(perm, 97.5, axis=0)
outside = [(l, o) for l, o, a, b in zip(LAGS, obs, lo, hi) if not np.isnan(o) and (o < a or o > b)]
print(f"--- 1. autocorrelation of D, lags 1-120: {len(outside)} of {int(np.sum(~np.isnan(obs)))} lags outside the shuffle band (about 5% = {0.05 * int(np.sum(~np.isnan(obs))):.0f} expected by chance)")
print("  outside: " + ", ".join(f"{l} ({15 * l} min) {o:+.2f}" for l, o in outside))
print("  at the scales named: hour (lag 4) {:+.3f}, quarter-day (lag 24) {:+.3f}, day (lag 96) {:+.3f}".format(obs[3], obs[23], obs[95] if len(obs) > 95 else float('nan')))
print("--- 2. the next window by the slow state and the last window")


def state(win):
    idx, nxt, last, slow = [], [], [], []
    for sg in segs:
        for j in range(win, len(sg) - 1):
            s = sg[j]
            idx.append(s + 1); nxt.append(D[s + 1]); last.append(D[s]); slow.append(float(np.mean(D[sg[j - win:j]])))
    return np.array(nxt), np.array(last), np.array(slow)


for win, nm in ((4, "hour (previous 4 windows)"), (24, "quarter-day (previous 24 windows)")):
    nxt, last, slow = state(win)
    if len(nxt) < 50:
        print(f"  {nm}: too few windows ({len(nxt)})")
        continue
    print(f"  slow = mean of D over the {nm}: {len(nxt)} windows")
    effs = {}
    for sn, sm in (("slow up", slow >= 0), ("slow down", slow < 0)):
        for ln, lm in (("last up", last >= 0), ("last down", last < 0)):
            q = sm & lm
            print(f"    {sn:9s} {ln:9s} n {int(q.sum()):4d} | mean next D {np.mean(nxt[q]):+.3f} | next window up {100.0 * np.mean(nxt[q] > 0):4.1f}%")
        effs[sn] = np.mean(nxt[sm & (last >= 0)]) - np.mean(nxt[sm & (last < 0)])
    diff = effs["slow up"] - effs["slow down"]
    sims = []
    for _ in range(300):
        sh = rng.permutation(len(nxt))
        n2 = nxt[sh]
        e_u = np.mean(n2[(slow >= 0) & (last >= 0)]) - np.mean(n2[(slow >= 0) & (last < 0)])
        e_d = np.mean(n2[(slow < 0) & (last >= 0)]) - np.mean(n2[(slow < 0) & (last < 0)])
        sims.append(e_u - e_d)
    print(f"    effect of the last window: in a slow-up state {effs['slow up']:+.3f}, in a slow-down state {effs['slow down']:+.3f}; difference {diff:+.3f} (shuffle {np.percentile(sims, 2.5):+.3f} to {np.percentile(sims, 97.5):+.3f})")
    sl = np.corrcoef(slow, nxt)[0, 1]
    print(f"    corr(slow state, next D) {sl:+.3f}")
