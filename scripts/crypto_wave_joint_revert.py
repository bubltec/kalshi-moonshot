"""The joint move of all coins: does it revert? (2026-11-19, user: "we need to do more analysis")

One question on the same markets (all eleven crypto series, archived Kalshi quotes, 2026-11-09 to the last archived day): every 60 s mark with 4 to 13 minutes left in a window where at least 6 coins quote, JOINT = the mean over coins of (YES mid now less 3 minutes ago) in each coin's typical minutes (rms one-second mid move times sqrt(60), from the seconds before the mark). Taken over ALL coins, not only those in the lane's band, and in both directions (YES and NO are the same curve mirrored). Marks are cut by size of |JOINT| (its own quantiles in this tape: under median, median to 80th, 80th to 95th, over 95th). For each cut, in the direction of the joint move: the mean of the NEXT 1 and 2 minutes' joint move (units; negative = it reverted), the share of coins that settled on the side the joint move pointed at, the mean ask of that side, and the net per $100 of buying that side at the ask and holding with the fee on every leg, and the same for the opposite side (fading it). Marks overlap in time; the counts are marks, not independent waves. Settings chosen after seeing the results are said to be so.
Run on the Mac: .venv/bin/python scripts/crypto_wave_joint_revert.py > studies/results/<date>-crypto-wave-joint-revert.txt"""
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
        for left in range(780, 230, -60):
            k = 900 - left
            rows = []
            for ticker, a in tk.items():
                m = a["m"]
                if np.isnan(m[[k - 180, k, k + 60 if k + 60 < 900 else k, k + 120 if k + 120 < 900 else k]]).any():
                    continue
                seen = a["d2"][:k][~np.isnan(a["d2"][:k])]
                if len(seen) < 120:
                    continue
                unit = float(np.sqrt(np.mean(seen)) * math.sqrt(60.0))
                if unit <= 0:
                    continue
                rows.append((a, unit))
            if len(rows) < 6:
                continue
            j = float(np.mean([(a["m"][k] - a["m"][k - 180]) / u for a, u in rows]))
            if k + 120 >= 900:
                continue
            n1 = float(np.mean([(a["m"][k + 60] - a["m"][k]) / u for a, u in rows]))
            n2 = float(np.mean([(a["m"][k + 120] - a["m"][k]) / u for a, u in rows]))
            sgn = 1.0 if j >= 0 else -1.0
            fol, fad = [], []
            for a, u in rows:
                yes_win = a["result"] == "yes"
                ya, yb = float(a["a"][k]), float(a["b"][k])
                if not (0 < ya <= 1 and 0 < yb <= 1):
                    continue
                if sgn > 0:
                    fol.append((ya, yes_win)); fad.append((1.0 - yb, not yes_win))
                else:
                    fol.append((1.0 - yb, not yes_win)); fad.append((ya, yes_win))
            events.append({"day": day, "J": abs(j), "n1": sgn * n1, "n2": sgn * n2, "fol": fol, "fad": fad})
n = len(events)
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {n} marks (60 s, 4 to 13 minutes left, 6+ coins quoting)")


def net(side):
    cost = pay = 0.0
    k = 0
    for p, w in side:
        if not (0.02 < p < 0.98):
            continue
        cost += p + 0.07 * p * (1.0 - p); pay += 1.0 if w else 0.0; k += 1
    return (100.0 * (pay - cost) / cost if cost else float("nan")), k, (pay / k if k else float("nan"))


J = np.array([e["J"] for e in events])
cuts = np.quantile(J, [0.5, 0.8, 0.95])
print(f"|joint 3-minute move| quantiles in typical minutes: median {cuts[0]:.2f}, 80th {cuts[1]:.2f}, 95th {cuts[2]:.2f}")
edges = [(-1, cuts[0], "under median"), (cuts[0], cuts[1], "median to 80th"), (cuts[1], cuts[2], "80th to 95th"), (cuts[2], 1e9, "over 95th")]
for lo, hi, nm in edges:
    sel = [e for e in events if lo < e["J"] <= hi]
    fol = [x for e in sel for x in e["fol"]]
    fad = [x for e in sel for x in e["fad"]]
    nf, kf, wf = net(fol)
    nd, kd, wd = net(fad)
    print(f"  {nm:15s} marks {len(sel):5d} | joint {np.mean([e['J'] for e in sel]):4.2f} | next 1 min {np.mean([e['n1'] for e in sel]):+.3f} | next 2 min {np.mean([e['n2'] for e in sel]):+.3f} (signed with the joint move; negative = reverted)")
    print(f"      follow: coins {kf:6d} win {100 * wf:5.1f}% mean ask {np.mean([p for p, _ in fol if 0.02 < p < 0.98]):.3f} net {nf:+6.1f} | fade: coins {kd:6d} win {100 * wd:5.1f}% mean ask {np.mean([p for p, _ in fad if 0.02 < p < 0.98]):.3f} net {nd:+6.1f}")
print("--- by day (over the 80th percentile only): follow net, fade net, marks")
for day in sorted(set(e["day"] for e in events)):
    sel = [e for e in events if e["day"] == day and e["J"] > cuts[1]]
    if len(sel) >= 10:
        print(f"  {day} marks {len(sel):4d} follow {net([x for e in sel for x in e['fol']])[0]:+6.1f} fade {net([x for e in sel for x in e['fad']])[0]:+6.1f} next 1 min {np.mean([e['n1'] for e in sel]):+.3f}")
