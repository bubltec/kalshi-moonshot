"""A favourite falls hard: does what came before say whether it comes back? (2026-11-18, user, after scripts/hard_fall_sell_or_wait.py found the price fair after the fall: "there must be more to the shape than just the fall. what came before?")

The same 15-minute markets and the same falls (a side whose bid was at or over 0.70 inside the last 15 s is first quoted under 0.50, a minute or more left; data/tape_archive quotes 2026-11-09 to 11-16, Kalshi's quotes only). Each fall is described by the side's own bid path from the market's first quote to 15 s before the fall:
  under even before   the side had already been quoted under 0.50 earlier in the market (it lost the lead before and won it back), or not (a favourite all along)
  straightness        the path's efficiency (net move over ground covered); split at the median of all falls
  reversals           turns of at least one typical minute of the market; split at the median
  time as favourite   the share of the market so far the side spent at or over 0.70; split at the median
Per contract, fee in: sell at once (the bid at the fall), sell 30 s later, hold to settlement. Nothing is fitted: every split is a median or a yes/no.
Run on the Mac: .venv/bin/python scripts/hard_fall_what_came_before.py > studies/results/<date>-hard-fall-what-came-before.txt"""
import csv
import glob
import math

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

FEE = 0.07
res = {r[0]: (float(r[1]), r[2]) for r in csv.reader(open("data/tape_archive/windows.csv")) if len(r) >= 3 and r[2] in ("yes", "no")}
ev = []
days = sorted(glob.glob("data/tape_archive/quotes/day=*/part-0.parquet"))
for f in days:
    t = pq.read_table(f, columns=["ts", "ticker", "bid", "ask"])
    t = t.filter(pc.match_substring(t["ticker"], "15M-"))
    df = t.to_pandas().sort_values(["ticker", "ts"])
    for ticker, g in df.groupby("ticker", sort=False):
        if ticker not in res or len(g) < 30:
            continue
        close, result = res[ticker]
        ts, yb, ya = g["ts"].to_numpy(), g["bid"].to_numpy(), g["ask"].to_numpy()
        ok = (yb > 0) & (ya > 0) & (ya <= 1) & (ya >= yb)
        ts, yb, ya = ts[ok], yb[ok], ya[ok]
        if len(ts) < 30:
            continue
        for side, b in (("yes", yb), ("no", 1.0 - ya)):
            high = pd.Series(b, index=pd.to_datetime(ts, unit="s")).rolling("15s").max().to_numpy()
            hit = np.flatnonzero((b < 0.50) & (high >= 0.70) & (close - ts >= 60.0) & (b > 0.02))
            if not len(hit):
                continue
            i = int(hit[0])
            j = int(np.searchsorted(ts, ts[i] - 15.0))
            if j < 20:
                continue
            sec = np.floor(ts[:j]).astype(np.int64)
            path = pd.Series(b[:j], index=sec).groupby(level=0).last().to_numpy()  # the side's bid, one a second, before the fall
            d = np.diff(path)
            if len(d) < 20:
                continue
            unit = float(np.sqrt(np.mean(d * d)) * math.sqrt(60.0))
            travel = float(np.abs(d).sum())
            if unit <= 0 or travel <= 0:
                continue
            turns, ref, way = 0, path[0], 0  # reversals of at least one typical minute
            for p in path[1:]:
                if way >= 0 and p <= ref - unit:
                    turns += way > 0
                    way, ref = -1, p
                elif way <= 0 and p >= ref + unit:
                    turns += way < 0
                    way, ref = 1, p
                elif (way > 0 and p > ref) or (way < 0 and p < ref):
                    ref = p
            k = np.searchsorted(ts, ts[i] + 30.0, side="right") - 1
            ev.append({"now": float(b[i]), "later": float(b[k]) if ts[i] + 30.0 <= close else None, "paid": result == side, "left": close - ts[i],
                       "under": bool((path < 0.50).any()), "eff": abs(float(path[-1] - path[0])) / travel, "turns": turns, "fav": float((path >= 0.70).mean())})


def net(p):
    return p - FEE * p * (1.0 - p)


def show(label, sel):
    n = len(sel)
    if not n:
        return
    lat = [net(e["later"]) for e in sel if e["later"] is not None]
    paid = sum(1 for e in sel if e["paid"])
    print(f"  {label:52s} falls {n:4d} | sell at once {np.mean([net(e['now']) for e in sel]):.3f} | 30 s later {np.mean(lat):.3f} | hold {paid / n:.3f} ({100.0 * paid / n:.0f}% came back) | hold less sell {paid / n - np.mean([net(e['now']) for e in sel]):+.3f}")


m_eff, m_turn, m_fav = (float(np.median([e[k] for e in ev])) for k in ("eff", "turns", "fav"))
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {len(ev)} first falls of a favourite with a path before them. Per contract, fee in. Medians: efficiency {m_eff:.2f}, reversals {m_turn:.0f}, share of the market as favourite {m_fav:.2f}")
show("all", ev)
print("--- had the side been under even before?")
show("yes: it lost the lead before and won it back", [e for e in ev if e["under"]])
show("no: a favourite all along", [e for e in ev if not e["under"]])
print("--- how straight was its path before the fall?")
show(f"straighter than the median ({m_eff:.2f})", [e for e in ev if e["eff"] > m_eff])
show("the median or more wandering", [e for e in ev if e["eff"] <= m_eff])
print("--- how many reversals before the fall?")
show(f"more than the median ({m_turn:.0f})", [e for e in ev if e["turns"] > m_turn])
show("the median or fewer", [e for e in ev if e["turns"] <= m_turn])
print("--- how long had it been a favourite?")
show(f"longer than the median ({m_fav:.0%} of the market so far)", [e for e in ev if e["fav"] > m_fav])
show("the median or shorter", [e for e in ev if e["fav"] <= m_fav])
print("--- a favourite all along, by the time left")
show("all along, over 3 min left", [e for e in ev if not e["under"] and e["left"] > 180])
show("all along, 1 to 3 min left", [e for e in ev if not e["under"] and e["left"] <= 180])
show("lost it before, over 3 min left", [e for e in ev if e["under"] and e["left"] > 180])
show("lost it before, 1 to 3 min left", [e for e in ev if e["under"] and e["left"] <= 180])
print("--- by the number of reversals before the fall (user: \"i think it depends on how many waves too, did we count?\")")
for lo, hi, name in ((0, 0, "none"), (1, 1, "1"), (2, 2, "2"), (3, 3, "3"), (4, 4, "4"), (5, 6, "5 or 6"), (7, 9, "7 to 9"), (10, 999, "10 or more")):
    show(f"{name} reversal(s)", [e for e in ev if lo <= e["turns"] <= hi])
print("DONE")
