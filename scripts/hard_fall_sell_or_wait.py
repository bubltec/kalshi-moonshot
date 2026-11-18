"""A favourite falls hard: sell at once, wait, or hold? (2026-11-18, user, on platinum 11:15: YES bought at 0.88, fell from 0.86 to 0.49 inside five seconds, the curve exit wanted out at about 0.39 and sold 27 s later at 0.20: "It should have sold it sooner but looks like it was a hard drop"; "we should have enough on tape to figure that out")

One question on the same moments: when a side that was a favourite (bid at or over 0.70 at some point in the previous 15 s) is first quoted under 0.50, with at least a minute left, what is it worth to sell at once, to sell 10, 20, 30 or 60 s later, or to hold to settlement?
The moments: the archived Kalshi quotes of every 15-minute market (data/tape_archive/quotes, days 2026-11-09 to 11-16, after the Monday rewrite), with the markets' results (windows.csv). A market's side counts once, at its first such fall. Kalshi's own quotes only.
Every sale is at the side's bid at that moment less the fee; holding is worth 1 if the side paid. 0.70 is the favourite lanes' buy floor, 0.50 is even: existing, structural numbers; the waits are the sizes of the exit's own wait (it ran 27 s on the platinum bet). Nothing is fitted.
By curve shape: how hard the fall was in the market's own units (the drop over the 15 s in typical minutes of that market before the fall; split at the median of all falls), and the time left.
Run on the Mac: .venv/bin/python scripts/hard_fall_sell_or_wait.py > studies/results/<date>-hard-fall-sell-or-wait.txt"""
import csv
import glob
import math
from collections import defaultdict

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

FEE = 0.07
WAITS = (10, 20, 30, 60)
res = {r[0]: (float(r[1]), r[2]) for r in csv.reader(open("data/tape_archive/windows.csv")) if len(r) >= 3 and r[2] in ("yes", "no")}
events = []
days = sorted(glob.glob("data/tape_archive/quotes/day=*/part-0.parquet"))
for f in days:
    t = pq.read_table(f, columns=["ts", "ticker", "bid", "ask"])
    t = t.filter(pc.match_substring(t["ticker"], "15M-"))
    df = t.to_pandas().sort_values(["ticker", "ts"])
    for ticker, g in df.groupby("ticker", sort=False):
        if ticker not in res or len(g) < 30:
            continue
        close, result = res[ticker]
        ts = g["ts"].to_numpy()
        yb, ya = g["bid"].to_numpy(), g["ask"].to_numpy()
        ok = (yb > 0) & (ya > 0) & (ya <= 1) & (ya >= yb)
        ts, yb, ya = ts[ok], yb[ok], ya[ok]
        if len(ts) < 30:
            continue
        mid = (yb + ya) / 2.0
        for side, b in (("yes", yb), ("no", 1.0 - ya)):
            high = pd.Series(b, index=pd.to_datetime(ts, unit="s")).rolling("15s").max().to_numpy()
            hit = np.flatnonzero((b < 0.50) & (high >= 0.70) & (close - ts >= 60.0) & (b > 0.02))
            if not len(hit):
                continue
            i = int(hit[0])
            # the market's typical minute before the fall: the rms one-second change of the mid over the earlier quotes, times sqrt(60)
            j = np.searchsorted(ts, ts[i] - 15.0)
            if j < 20:
                continue
            sec = np.floor(ts[:j]).astype(np.int64)
            last = pd.Series(mid[:j], index=sec).groupby(level=0).last()
            d = np.diff(last.to_numpy())
            unit = float(np.sqrt(np.mean(d * d)) * math.sqrt(60.0)) if len(d) >= 20 else 0.0
            if unit <= 0:
                continue
            later = {}
            for w in WAITS:
                k = np.searchsorted(ts, ts[i] + w, side="right") - 1
                later[w] = float(b[k]) if ts[i] + w <= close else None
            events.append({"left": close - ts[i], "now": float(b[i]), "high": float(high[i]), "units": (float(high[i]) - float(b[i])) / unit, "later": later, "paid": result == side})


def net(p):
    return p - FEE * p * (1.0 - p)


med = float(np.median([e["units"] for e in events])) if events else 0.0


def show(label, sel):
    if not sel:
        print(f"  {label:42s} falls    0")
        return
    n = len(sel)
    now = np.mean([net(e["now"]) for e in sel])
    paid = sum(1 for e in sel if e["paid"])
    line = f"  {label:42s} falls {n:4d} | fell from {np.mean([e['high'] for e in sel]):.2f} to {np.mean([e['now'] for e in sel]):.2f} | sell at once {now:.3f}"
    for w in WAITS:
        got = [(net(e["later"][w]), net(e["now"])) for e in sel if e["later"][w] is not None]
        if got:
            better = sum(1 for a, b0 in got if a > b0 + 1e-9)
            line += f" | +{w}s {np.mean([a for a, _ in got]):.3f} ({100.0 * better / len(got):.0f}% better)"
    line += f" | hold {paid / n:.3f} ({100.0 * paid / n:.0f}% paid)"
    print(line)


print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}, every 15-minute market with a result; {len(events)} first falls of a favourite (bid at or over 0.70 inside the last 15 s, now under 0.50, a minute or more left). Values are per contract, fee in. Median fall: {med:.1f} typical minutes of the market")
show("all", events)
print("--- by how hard the fall was, in the market's own typical minutes")
show(f"harder than the median ({med:.1f})", [e for e in events if e["units"] > med])
show("the median or softer", [e for e in events if e["units"] <= med])
print("--- by the time left")
show("over 5 min left", [e for e in events if e["left"] > 300])
show("3 to 5 min left", [e for e in events if 180 < e["left"] <= 300])
show("1 to 3 min left", [e for e in events if e["left"] <= 180])
print("--- hard falls only, by the time left")
show("hard, over 3 min left", [e for e in events if e["units"] > med and e["left"] > 180])
show("hard, 1 to 3 min left", [e for e in events if e["units"] > med and e["left"] <= 180])
print("DONE")
