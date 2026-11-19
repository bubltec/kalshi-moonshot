"""The shape of the "all coins lose" waves on the full tape. (2026-11-18, user: "we need to look at the full tape and the shape of the waves when we have these 'all coins lose' situations")

One question on the same markets: take every 15-minute window of the archived Kalshi quotes (all eleven crypto series, 2026-11-09 to the last archived day) and, at every 60 s mark with 4 to 13 minutes left, the coins whose YES (or NO) side is at 0.60 to 0.85 (the lane's band): the FAVOURITE SET of that way. A set of four or more is a candidate wave. The first mark a window's set reaches four counts (one a window and way). Afterwards each set is labelled by how it settled: the share of its coins whose side lost. "All lose" = 75% or more lost, "all win" = 25% or fewer lost, the rest mixed. Labels are known only afterwards; the point is to see what the set's CURVES looked like beforehand.
Readings, all from Kalshi's own quotes, in each coin's typical minutes (rms one-second mid move times sqrt(60), from the seconds before the mark), averaged over the set: slope3 (side mid now less 3 minutes ago), range5 (high less low over 5 minutes), eff5 (path efficiency over 5 minutes), flips5 (crossings of 0.50 by the mid in 5 minutes), disp (spread of the set's side mids, in cents), age (minutes since the set formed: how long ago the first of its coins crossed 0.60). And the set's MEAN PATH, the side mid averaged over its coins at -5, -4 ... +5 minutes from the mark (the shape of the wave itself, before and after).
Reported: how many sets, by label; the mean of each reading by label; the mean path by label; and the share of sets that went "all lose" by fifths of each reading (a study's cut, not a live rule). No coin is named: the sets are counted, not named. Settings chosen after seeing the results are said to be so.
Run on the Mac: .venv/bin/python scripts/crypto_wave_shapes.py > studies/results/<date>-crypto-wave-shapes.txt"""
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
        for way in ("yes", "no"):
            for left in MARKS:
                k = 900 - left
                if k < 300:
                    continue
                coins = []
                for ticker, a in tk.items():
                    ask = a["a"][k] if way == "yes" else 1.0 - a["b"][k]
                    if np.isnan(ask) or not (LO <= ask <= HI):
                        continue
                    seen = a["d2"][:k][~np.isnan(a["d2"][:k])]
                    if len(seen) < 120:
                        continue
                    unit = float(np.sqrt(np.mean(seen)) * math.sqrt(60.0))
                    if unit <= 0:
                        continue
                    side = a["m"] if way == "yes" else 1.0 - a["m"]
                    if np.isnan(side[k - 300:k + 1]).any():
                        continue
                    coins.append((ticker, a, side, unit))
                if len(coins) < 4:
                    continue
                lost = [a["result"] != way for _, a, _, _ in coins]
                w5 = [s[k - 300:k + 1] for _, _, s, _ in coins]
                moves = [np.abs(np.diff(x)).sum() for x in w5]
                paths = []
                for off in OFFS:
                    j = k + off
                    if 0 <= j < 900:
                        paths.append(float(np.nanmean([s[j] for _, _, s, _ in coins])))
                    else:
                        paths.append(float("nan"))
                crossed = []
                for _, _, s, _ in coins:
                    above = np.flatnonzero(s[:k + 1] >= LO)
                    crossed.append(k - int(above[0]) if len(above) else 0)
                events.append({"day": day, "way": way, "left": left, "n": len(coins), "lose": float(np.mean(lost)),
                               "slope3": float(np.mean([(s[k] - s[k - 180]) / u for _, _, s, u in coins])),
                               "range5": float(np.mean([(x.max() - x.min()) / u for x, (_, _, _, u) in zip(w5, coins)])),
                               "eff5": float(np.mean([(abs(x[-1] - x[0]) / mv) if mv > 0 else 1.0 for x, mv in zip(w5, moves)])),
                               "flips5": float(np.mean([int(np.sum(np.diff((x >= 0.5).astype(int)) != 0)) for x in w5])),
                               "disp": float(np.std([s[k] for _, _, s, _ in coins]) * 100.0),
                               "age": float(max(crossed)) / 60.0, "paths": paths})
                break  # the first mark where the set reaches four counts: one a window and way
n = len(events)
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {n} favourite sets of four or more coins (one a window and way) at the first 60 s mark with 4 to 13 minutes left, the lane's band {LO:.2f} to {HI:.2f}")
labels = {"all lose (75% or more lost)": lambda e: e["lose"] >= 0.75, "mixed": lambda e: 0.25 < e["lose"] < 0.75, "all win (25% or fewer lost)": lambda e: e["lose"] <= 0.25}
for name, test in labels.items():
    sel = [e for e in events if test(e)]
    print(f"  {name:30s} sets {len(sel):4d} ({100.0 * len(sel) / max(n, 1):4.1f}%) | coins in a set {np.mean([e['n'] for e in sel]) if sel else 0:4.1f} | mean share lost {100.0 * np.mean([e['lose'] for e in sel]) if sel else 0:5.1f}%")
READS = ("slope3", "range5", "eff5", "flips5", "disp", "age", "left", "n")
print("--- the mean of each reading by label")
print("  " + " " * 30 + "".join(f"{r:>9s}" for r in READS))
for name, test in labels.items():
    sel = [e for e in events if test(e)]
    if sel:
        print(f"  {name:30s}" + "".join(f"{np.mean([e[r] for e in sel]):9.2f}" for r in READS))
print("--- the set's mean side mid from 5 minutes before the mark to 5 after, by label (the wave's shape)")
print("  " + " " * 30 + "".join(f"{o // 60:>+7d}m" for o in OFFS))
for name, test in labels.items():
    sel = [e for e in events if test(e)]
    if sel:
        print(f"  {name:30s}" + "".join(f"{np.nanmean([e['paths'][i] for e in sel]):8.3f}" for i in range(len(OFFS))))
print("--- the share of sets that went 'all lose', by fifths of each reading known at the mark")
base = sum(1 for e in events if e["lose"] >= 0.75) / max(n, 1)
print(f"  all sets: {100.0 * base:.1f}% went all lose; mean share of the set's coins lost {100.0 * np.mean([e['lose'] for e in events]):.1f}%")
for r in ("slope3", "range5", "eff5", "flips5", "disp", "age", "left", "n"):
    vals = np.array([e[r] for e in events])
    cuts = np.quantile(vals, [0.2, 0.4, 0.6, 0.8])
    print(f"  by {r}")
    edges = [-np.inf, *cuts.tolist(), np.inf]
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = [e for e in events if lo < e[r] <= hi] if lo != -np.inf else [e for e in events if e[r] <= hi]
        if len(sel) < 15:
            continue
        nm = f"under {hi:.2f}" if lo == -np.inf else f"over {lo:.2f}" if hi == np.inf else f"{lo:.2f} to {hi:.2f}"
        print(f"    {nm:20s} sets {len(sel):4d} | all lose {100.0 * np.mean([e['lose'] >= 0.75 for e in sel]):5.1f}% | all win {100.0 * np.mean([e['lose'] <= 0.25 for e in sel]):5.1f}% | mean share lost {100.0 * np.mean([e['lose'] for e in sel]):5.1f}%")
print("--- waiting one minute: the set's mean side mid one minute AFTER the mark less its value at the mark (known 60 s after the buy would have been placed)")
i0, i1, i2 = OFFS.index(0), OFFS.index(60), OFFS.index(120)
for nm, lo, hi in (("fell 0.05 or more", -9, -0.05), ("fell under 0.05", -0.05, 0.0), ("rose under 0.03", 0.0, 0.03), ("rose 0.03 or more", 0.03, 9)):
    sel = [e for e in events if lo <= e["paths"][i1] - e["paths"][i0] < hi]
    if len(sel) >= 15:
        print(f"    {nm:20s} sets {len(sel):4d} | all lose {100.0 * np.mean([e['lose'] >= 0.75 for e in sel]):5.1f}% | all win {100.0 * np.mean([e['lose'] <= 0.25 for e in sel]):5.1f}% | mean share lost {100.0 * np.mean([e['lose'] for e in sel]):5.1f}% | mean side mid at the mark {np.mean([e['paths'][i0] for e in sel]):.3f}, a minute later {np.mean([e['paths'][i1] for e in sel]):.3f}")
print("--- waiting two minutes: the same, two minutes after the mark")
for nm, lo, hi in (("fell 0.05 or more", -9, -0.05), ("fell under 0.05", -0.05, 0.0), ("rose under 0.05", 0.0, 0.05), ("rose 0.05 or more", 0.05, 9)):
    sel = [e for e in events if lo <= e["paths"][i2] - e["paths"][i0] < hi]
    if len(sel) >= 15:
        print(f"    {nm:20s} sets {len(sel):4d} | all lose {100.0 * np.mean([e['lose'] >= 0.75 for e in sel]):5.1f}% | all win {100.0 * np.mean([e['lose'] <= 0.25 for e in sel]):5.1f}% | mean share lost {100.0 * np.mean([e['lose'] for e in sel]):5.1f}% | mean side mid at the mark {np.mean([e['paths'][i0] for e in sel]):.3f}, two minutes later {np.mean([e['paths'][i2] for e in sel]):.3f}")
print("DONE")
