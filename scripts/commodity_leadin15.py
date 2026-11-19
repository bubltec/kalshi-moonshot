"""Does the shape of the commodity markets in the 15 minutes before predict how the next window's favourites pay? (2026-11-18, user: "It's probably a 15 minute lead in, and it seems like most all commodity markets get affected")
One question on the bets of scripts/commodity_favourite_by_shape.py (a commodity side first quoted from 0.60 to 0.92 at a 30 s mark with 3 to 13 min left, one a market and side, held to settlement, taker fee, one contract each). The lead-in of a bet is the PREVIOUS 15-minute window: every commodity market that closed at the start of the bet's window, pooled across all commodities (the user's "most all markets get affected": one reading for the whole group, nothing keyed on a market name):
  unit       median typical minute of those markets (rms one-second mid move times sqrt(60)), cents
  flips      median changes of favourite
  contested  median share of seconds the mid sat between 0.30 and 0.70
  moved      share of those markets that changed favourite at least twice (how many got hit at once)
  last       the lead-in window's own net per $100 on its favourites (outcome-based: does a good window follow a good window)
Each reading is cut at the terciles of the windows studied (a study's cut, not a live rule). Per cut: windows, bets, win rate against the average ask, net per $100 staked, days positive. The lead-in is lagged one window, so every reading is known before the bet. Settings chosen after seeing the results are said to be so.
Tape: data/tape_archive/quotes, 2026-11-09 to the last archived day. Run on the Mac: .venv/bin/python scripts/commodity_leadin15.py > studies/results/<date>-commodity-leadin15.txt"""
import csv
import glob
import math

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

FEE = 0.07
LO, HI = 0.60, 0.92
SERIES = ("KXBRENT15M", "KXWTI15M", "KXNATGAS15M", "KXGOLD15M", "KXSILVER15M", "KXCOPPER15M", "KXPLATINUM15M", "KXPALLADIUM15M")
res = {r[0]: (float(r[1]), r[2]) for r in csv.reader(open("data/tape_archive/windows.csv")) if len(r) >= 3 and r[2] in ("yes", "no")}
bets, mk = [], []
days = sorted(glob.glob("data/tape_archive/quotes/day=*/part-0.parquet"))
for f in days:
    day = f.split("day=")[1][:10]
    t = pq.read_table(f, columns=["ts", "ticker", "bid", "ask"])
    t = t.filter(pc.match_substring(t["ticker"], "15M-"))
    df = t.to_pandas()
    df = df[df["ticker"].str.split("-").str[0].isin(SERIES)].sort_values(["ticker", "ts"])
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
        fav = pd.Series(np.where(yb >= 0.50, 1.0, np.where(ya <= 0.50, -1.0, np.nan))).ffill().to_numpy()
        change = np.zeros(len(ts), dtype=bool)
        change[1:] = (fav[1:] != fav[:-1]) & ~np.isnan(fav[1:]) & ~np.isnan(fav[:-1])
        cts = ts[change]
        secs = np.arange(close - 900, close)
        at = np.searchsorted(ts, secs, side="right") - 1
        have = at >= 0
        msec = np.where(have, mid[np.maximum(at, 0)], np.nan)
        d = np.where(have[1:] & have[:-1], np.diff(msec), np.nan)
        dd = d[~np.isnan(d)]
        m = msec[~np.isnan(msec)]
        if len(dd) < 300 or len(m) < 300:
            continue
        mk.append({"slot": int(close // 900), "unit": float(np.sqrt(np.mean(dd * dd)) * math.sqrt(60.0)) * 100.0, "flips": float(len(cts)), "contested": float(np.mean((m > 0.30) & (m < 0.70)))})
        d2 = d * d
        taken = set()
        for left in range(780, 170, -30):
            now = close - left
            k = 900 - left
            i = int(np.searchsorted(ts, now, side="right") - 1)
            if i < 20 or now - ts[i] > 30 or k < 300:
                continue
            seen = d2[:k][~np.isnan(d2[:k])]
            if len(seen) < 120 or float(np.sqrt(np.mean(seen))) <= 0:
                continue
            for side, a in (("yes", ya[i]), ("no", 1.0 - yb[i])):
                if side in taken or not (LO <= a <= HI):
                    continue
                taken.add(side)
                a = float(a)
                won = result == side
                bets.append({"slot": int(close // 900), "day": day, "ask": a, "won": won, "net": (1.0 if won else 0.0) - a - FEE * a * (1 - a)})
b = pd.DataFrame(bets)
m = pd.DataFrame(mk)
lead = m.groupby("slot").agg(unit=("unit", "median"), flips=("flips", "median"), contested=("contested", "median"), moved=("flips", lambda s: float((s >= 2).mean())), mkts=("unit", "size"))
bs = b.groupby("slot").agg(net=("net", "sum"), stake=("ask", "sum"))
lead["last"] = 100.0 * (bs["net"] / bs["stake"])
L = lead[lead["mkts"] >= 5].copy()
L.index = L.index + 1  # a reading is applied to the NEXT window
b = b.join(L, on="slot", how="inner").dropna(subset=["unit", "flips", "contested", "moved", "last"])
ndays = b["day"].nunique()
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {len(b)} commodity favourites in {b['slot'].nunique()} windows with a lead-in window of 5 or more markets; one contract each, held, taker fee.")


def show(label, sel):
    if len(sel) < 40:
        print(f"  {label:34s} bets {len(sel):5d}")
        return
    per_day = sel.groupby("day")["net"].sum()
    se = 100.0 * sel.groupby("slot")["net"].sum().std() * math.sqrt(sel["slot"].nunique()) / sel["ask"].sum()
    print(f"  {label:34s} windows {sel['slot'].nunique():4d} bets {len(sel):5d} | win {100.0 * sel['won'].mean():5.1f}% vs ask {100.0 * sel['ask'].mean():5.1f}c | net {sel['net'].sum():+8.2f} ({100.0 * sel['net'].sum() / sel['ask'].sum():+5.1f} per $100, window-clustered se {se:.1f}) | days positive {int((per_day > 0).sum())} of {ndays}")


show("all", b)
w = b.drop_duplicates("slot").set_index("slot")
for col in ("unit", "flips", "contested", "moved", "last"):
    print(f"--- by the lead-in's {col} (terciles of windows)")
    lo, hi = w[col].quantile([1 / 3, 2 / 3])
    show(f"{col} low (up to {lo:.2f})", b[b[col] <= lo])
    show(f"{col} middle", b[(b[col] > lo) & (b[col] <= hi)])
    show(f"{col} high (over {hi:.2f})", b[b[col] > hi])
print("DONE")
