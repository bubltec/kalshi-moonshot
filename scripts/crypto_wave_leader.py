"""Is there a lead coin? (2026-11-19, user: "is there a leader? like BTC? ... every other coin follows suit within a few minutes")

One question on the archived Kalshi quote curves (eleven crypto series, 2026-11-09 to the last archived day): does one series' YES-mid move lead the others'? Per 15-minute window and series, the YES mid at every 30 s from 60 s into the window; r = the 30 s change in the coin's own typical minutes (rms one-second move of that window's earlier seconds is not used here: the whole window's rms, so it is a study measure, not a live reading). For each ordered pair (A, B) and lag L of 1 to 6 steps (30 s to 3 minutes) the correlation of A's change now with B's change L steps later, pooled over all windows; lead score of A = the mean over the other series B and lags 1 to 6 of corr(A then, B later) less corr(B then, A later). Positive = A moves first. Also, what the lead says: the next 1, 2 and 3 minutes of every OTHER coin after A's last 1-minute move, signed with it, in typical minutes (average over windows and over other coins; zero means no follow). Series are named because the question is about named leaders; a live rule must not be keyed on a name. Settings chosen after seeing the results are said to be so.
Run on the Mac: .venv/bin/python scripts/crypto_wave_leader.py > studies/results/<date>-crypto-wave-leader.txt"""
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
        mat = {}
        for ticker, a in tk.items():
            m = a["m"][60::30]
            if np.isnan(m).any() or len(m) < 28:
                continue
            u = float(np.sqrt(np.nanmean(a["d2"])) * math.sqrt(60.0))
            if u <= 0:
                continue
            mat[ticker.split("-")[0]] = np.diff(m) / u
        if len(mat) >= 6:
            events.append(mat)
n = len(events)
names = sorted(SERIES)
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {n} windows with 6+ coins; 30 s steps")
LAGS = range(1, 7)


def pooled(a, b, lag):
    xs, ys = [], []
    for mat in events:
        if a in mat and b in mat:
            x, y = mat[a], mat[b]
            xs.append(x[:-lag]); ys.append(y[lag:])
    if not xs:
        return float("nan")
    x, y = np.concatenate(xs), np.concatenate(ys)
    return float(np.corrcoef(x, y)[0, 1])


print("--- corr of one coin's 30 s move now with another's L steps later; lead score = mean over other coins and L=1..6 of (A then B later) less (B then A later)")
rows = []
for a in names:
    sc, same = [], []
    for b in names:
        if a == b:
            continue
        f = [pooled(a, b, L) for L in LAGS]
        r = [pooled(b, a, L) for L in LAGS]
        sc.append(np.nanmean(f) - np.nanmean(r))
        same.append(np.nanmean(f))
    rows.append((a, float(np.nanmean(sc)), float(np.nanmean(same))))
for a, s, f in sorted(rows, key=lambda x: -x[1]):
    print(f"  {a:10s} lead score {s:+.4f} | mean corr of its move with the others' later moves {f:+.4f}")
def same_time(a, b):
    xs = [m[a] for m in events if a in m and b in m]
    ys = [m[b] for m in events if a in m and b in m]
    return float(np.corrcoef(np.concatenate(xs), np.concatenate(ys))[0, 1]) if xs else float("nan")


print("--- same-time (lag 0) corr of 30 s moves, mean over pairs:", f"{np.nanmean([same_time(a, b) for a in names for b in names if a < b]):.3f}")
print("--- series present in the windows:", ", ".join(f"{a} {sum(1 for m in events if a in m)}" for a in names))
print("--- in each window, which series made the biggest 3-minute joint-direction move first (the earliest 30 s step where its cumulative move reaches 1 typical minute, in the window's overall direction): how often each series is first (equal shares = any coin can be first)")
first = {a: 0 for a in names}
tot = 0
for mat in events:
    cum = {a: np.cumsum(x) for a, x in mat.items()}
    tot_move = float(np.mean([c[-1] for c in cum.values()]))
    if abs(tot_move) < 1.5:
        continue
    sg = 1.0 if tot_move > 0 else -1.0
    hit = {a: int(np.argmax(sg * c >= 1.0)) for a, c in cum.items() if (sg * c >= 1.0).any()}
    if len(hit) < 6:
        continue
    t0 = min(hit.values())
    firsts = [a for a, t in hit.items() if t == t0]
    for a in firsts:
        first[a] += 1.0 / len(firsts)
    tot += 1
print(f"  windows with a joint move of 1.5 typical minutes or more: {tot}; expected share if any coin: {100.0 / max(1, sum(1 for a in names if any(a in m for m in events))):.1f}%")
for a in sorted(names, key=lambda x: -first[x]):
    print(f"  {a:10s} first in {first[a]:6.1f} windows ({100.0 * first[a] / max(tot, 1):4.1f}%)")
print("--- after a coin's last 1-minute move (two steps) of 1.0 typical minute or more either way: the other coins' next 1, 2, 3 minutes, signed with it (typical minutes), marks/coin counts")
for a in names:
    out = [[], [], []]
    cnt = 0
    for mat in events:
        if a not in mat:
            continue
        x = mat[a]
        for i in range(2, len(x) - 6):
            mv = x[i - 2:i].sum()
            if abs(mv) < 1.0:
                continue
            cnt += 1
            s = 1.0 if mv > 0 else -1.0
            for j, steps in enumerate((2, 4, 6)):
                v = [s * mat[b][i:i + steps].sum() for b in mat if b != a]
                out[j].append(float(np.mean(v)))
    if cnt:
        print(f"  {a:10s} moves {cnt:5d} | others next 1 min {np.mean(out[0]):+.3f} | 2 min {np.mean(out[1]):+.3f} | 3 min {np.mean(out[2]):+.3f}")
