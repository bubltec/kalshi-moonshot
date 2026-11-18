"""Last four minutes: buy a side at 30 cents or under with a resting bid, rest a sale at 50 cents. Does the sale fill often enough? (2026-11-18, user: "in the last 4 mins of the market there are opportunities in markets where the price is volatile ... fee-free buys, and only trying to buy 30c or less sides, then put a sell order for 50c ... on markets that have crossed the strike in the last 5 mins at least once ... capable of making 20c profits without trying to win the market")

One question on the same buys: a side whose ask is at or under 0.30 with four minutes or less left, in a market whose favourite changed at least once in the five minutes before; bought there, a sale rests at 0.50. Against holding the same buy to settlement.
The buys: the archived Kalshi quotes of every 15-minute market (data/tape_archive/quotes, days from 2026-11-09, after the Monday rewrite), results from windows.csv. A market's side counts once, at its first such moment. Kalshi's own quotes only.
A change of favourite (the strike crossed, read on Kalshi's own curve): YES bid at or over 0.50 makes YES the favourite, YES ask at or under 0.50 makes NO; in between, the last one stands. So a one-cent flicker round 0.50 is not a crossing.
Fills: the buy is a resting bid, no fee. If the ask fell through 0.30 the buy is at 0.30; if the side was already under it when the rule first held, at the ask then (a resting bid is not sure to fill there: the column with the taker fee on the buy is that case). The sale fills at 0.50, no fee, when the side's BID is at or over 0.50 at a later quote; if it never is, the bet settles (1 or 0).
0.30, 0.50, four and five minutes are the user's numbers, stated before the run. Nothing is fitted.
By curve shape: the number of changes of favourite in those five minutes; the reach (how far 0.50 is from the buy, in the market's own typical minutes, over the square root of the minutes left: under 1 means an ordinary move gets there); the time left; and the same buys in markets with NO change of favourite, to see what the crossing condition adds.
Run on the Mac: .venv/bin/python scripts/late_cheap_side_round_trip.py > studies/results/<date>-late-cheap-side-round-trip.txt"""
import csv
import glob
import math

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

FEE = 0.07
BUY, SELL, LAST, BACK = 0.30, 0.50, 240.0, 300.0
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
        ok = (yb > 0) & (ya > 0) & (ya <= 1) & (ya >= yb) & (ts < close)
        ts, yb, ya = ts[ok], yb[ok], ya[ok]
        if len(ts) < 30:
            continue
        mid = (yb + ya) / 2.0
        # the favourite at each quote (1 yes, -1 no), the last one standing while the spread straddles 0.50
        fav = pd.Series(np.where(yb >= 0.50, 1.0, np.where(ya <= 0.50, -1.0, np.nan))).ffill().to_numpy()
        change = np.zeros(len(ts))
        change[1:] = (fav[1:] != fav[:-1]) & ~np.isnan(fav[1:]) & ~np.isnan(fav[:-1])
        cum = np.cumsum(change)
        back = cum - cum[np.searchsorted(ts, ts - BACK)] + change[np.searchsorted(ts, ts - BACK)]  # changes inside the five minutes before each quote
        late = (close - ts <= LAST)
        for side, b, a in (("yes", yb, ya), ("no", 1.0 - ya, 1.0 - yb)):
            hit = np.flatnonzero(late & (a <= BUY) & (a > 0.01))
            if not len(hit):
                continue
            for crossed in (True, False):
                sel = hit[back[hit] >= 1] if crossed else hit[back[hit] < 1]
                if not len(sel):
                    continue
                i = int(sel[0])
                if i < 20:
                    continue
                buy = BUY if a[i - 1] > BUY else float(a[i])
                sec = np.floor(ts[:i]).astype(np.int64)
                d = np.diff(pd.Series(mid[:i], index=sec).groupby(level=0).last().to_numpy())
                unit = float(np.sqrt(np.mean(d * d)) * math.sqrt(60.0)) if len(d) >= 20 else 0.0
                if unit <= 0:
                    continue
                left = close - ts[i]
                sold = np.flatnonzero(b[i + 1:] >= SELL)
                events.append({"crossed": crossed, "n_cross": int(back[i]), "buy": buy, "left": left, "reach": (SELL - buy) / (unit * math.sqrt(max(left, 1.0) / 60.0)),
                               "sold": bool(len(sold)), "wait": float(ts[i + 1 + sold[0]] - ts[i]) if len(sold) else None, "paid": result == side, "day": f.split("day=")[1][:10]})


def show(label, sel):
    if not sel:
        print(f"  {label:44s} buys    0")
        return
    n = len(sel)
    sold = sum(1 for e in sel if e["sold"])
    trip = [(SELL if e["sold"] else float(e["paid"])) - e["buy"] for e in sel]
    hold = [float(e["paid"]) - e["buy"] for e in sel]
    fee = sum(FEE * e["buy"] * (1.0 - e["buy"]) for e in sel)
    beat = sum(1 for e in sel if e["sold"] and not e["paid"])
    lost = sum(1 for e in sel if e["sold"] and e["paid"])
    need = np.mean([e["buy"] for e in sel]) / SELL
    waits = [e["wait"] for e in sel if e["sold"]]
    print(f"  {label:44s} buys {n:4d} | avg buy {np.mean([e['buy'] for e in sel]):.3f} | sale filled {sold:4d} ({100.0 * sold / n:4.1f}%, needs about {100.0 * need:.0f}%) | round trip {sum(trip):+8.2f} ({np.mean(trip):+.3f} each) | with the fee on the buy {sum(trip) - fee:+8.2f} | hold {sum(hold):+8.2f} ({100.0 * sum(1 for e in sel if e['paid']) / n:4.1f}% paid) | beat hold {beat:4d}, lost to it {lost:4d}" + (f" | median wait {np.median(waits):.0f} s" if waits else ""))


yes = [e for e in events if e["crossed"]]
no = [e for e in events if not e["crossed"]]
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}, every 15-minute market with a result; {len(yes)} buys (a side's first ask at or under {BUY:.2f} with 4 minutes or less left, the favourite changed in the 5 minutes before). Dollars are for one contract a buy, no fee on either leg unless said.")
show("all", yes)
show("same buys, NO change of favourite before", no)
print("--- by the number of changes of favourite in the five minutes before")
show("1 change", [e for e in yes if e["n_cross"] == 1])
show("2 changes", [e for e in yes if e["n_cross"] == 2])
show("3 or 4 changes", [e for e in yes if 3 <= e["n_cross"] <= 4])
show("5 or more changes", [e for e in yes if e["n_cross"] >= 5])
print("--- by the reach: the way to 0.50 in the market's typical minutes, over the square root of the minutes left")
for lo, hi in ((0, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, 4.0), (4.0, 1e9)):
    show(f"reach {lo:g} to {hi:g}" if hi < 1e9 else f"reach over {lo:g}", [e for e in yes if lo <= e["reach"] < hi])
print("--- by the time left at the buy")
for lo, hi in ((180, 240), (120, 180), (60, 120), (0, 60)):
    show(f"{lo // 60} to {hi // 60} min left", [e for e in yes if lo < e["left"] <= hi])
print("--- by the buy price")
show("bought at 0.30 (the ask fell through it)", [e for e in yes if e["buy"] >= BUY - 1e-9])
show("already under 0.30: 0.20 to 0.30", [e for e in yes if 0.20 <= e["buy"] < BUY - 1e-9])
show("already under 0.30: 0.10 to 0.20", [e for e in yes if 0.10 <= e["buy"] < 0.20])
show("already under 0.30: under 0.10", [e for e in yes if e["buy"] < 0.10])
print("--- by day (is it one day's result?)")
for day in sorted({e["day"] for e in yes}):
    show(day, [e for e in yes if e["day"] == day])
print("DONE")
