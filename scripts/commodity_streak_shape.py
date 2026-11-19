"""What did the commodity markets look like in the 3 hours before a run of wins and before a cluster of losses? (2026-11-18, user: "Don't run what we lost or won. When we lost multiple in a period or won a streak like this what was the shape of the market for the past 3 hours?")

The bets are the commodity favourites of scripts/commodity_favourite_by_shape.py (a side first quoted from 0.60 to 0.92 at a 30 s mark with 3 to 13 min left, one a market and side, held to settlement, taker fee). They only mark the periods. Periods are clock hours (ET-free: UTC hour of the buy), chosen after seeing the data and said to be so:
  loss hours   the worst fifth of hours by net per $100 staked (hours with 15+ bets: about 35 bets an hour across 8 markets, so every hour has losses)
  clean hours  the best fifth of hours by net per $100 staked
  other        the middle three fifths
For each period the shape is read from Kalshi's own quotes over the 3 hours BEFORE the hour starts (no bet of the hour is in it): every commodity market that closed in that window, one summary per market, then the median over markets:
  unit       typical minute of the market (rms one-second move of the mid times sqrt(60)), cents
  flips      changes of favourite in the market
  contested  share of seconds the mid sat between 0.30 and 0.70
  decisive   mean |mid - 0.5| over the market's seconds
  eff        path efficiency of the mid over the whole market (net move over the sum of one-second moves)
  range      high less low of the mid
  markets    how many markets closed in the window
Also each hour's own typical minute against that market's own 8-day median is not used: the comparison is between period kinds. Reported per kind: hours, bets, losses, and the median of each reading (and the 25th to 75th share). Nothing is keyed on a market name; the pooled commodity markets are the sample.
Tape: data/tape_archive/quotes, 2026-11-09 to the last archived day, results windows.csv.
Run on the Mac: .venv/bin/python scripts/commodity_streak_shape.py > studies/results/<date>-commodity-streak-shape.txt"""
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
        unit = float(np.sqrt(np.mean(dd * dd)) * math.sqrt(60.0)) * 100.0
        moves = float(np.abs(dd).sum())
        mk.append({"close": close, "unit": unit, "flips": float(len(cts)), "contested": float(np.mean((m > 0.30) & (m < 0.70))), "decisive": float(np.mean(np.abs(m - 0.5))),
                   "eff": (abs(m[-1] - m[0]) / moves) if moves > 0 else 1.0, "range": float(m.max() - m.min())})
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
                bets.append({"t": now, "won": won, "net": (1.0 if won else 0.0) - a - FEE * a * (1 - a), "ask": a})

b = pd.DataFrame(bets)
m = pd.DataFrame(mk).sort_values("close").reset_index(drop=True)
b["hour"] = (b["t"] // 3600).astype(int)
g = b.groupby("hour").agg(n=("won", "size"), lost=("won", lambda s: int((~s).sum())), net=("net", "sum"), stake=("ask", "sum"))
g = g[g["n"] >= 15].copy()
g["per100"] = 100.0 * g["net"] / g["stake"]
q20, q80 = g["per100"].quantile(0.2), g["per100"].quantile(0.8)
g["kind"] = np.where(g["per100"] <= q20, "loss hours (worst fifth by net per $100)", np.where(g["per100"] >= q80, "clean hours (best fifth by net per $100)", "other hours"))
READ = ("unit", "flips", "contested", "decisive", "eff", "range")
rows = []
for hour, r in g.iterrows():
    h0 = hour * 3600
    w = m[(m["close"] > h0 - 3 * 3600) & (m["close"] <= h0)]
    if len(w) < 20:
        continue
    row = {"hour": hour, "kind": r["kind"], "n": r["n"], "lost": r["lost"], "markets": len(w)}
    for c in READ:
        row[c] = float(w[c].median())
    rows.append(row)
p = pd.DataFrame(rows)
print(f"window: {pd.to_datetime(b['t'].min(), unit='s'):%m-%d %H:%M} to {pd.to_datetime(b['t'].max(), unit='s'):%m-%d %H:%M} UTC; {len(b)} commodity favourites over {len(g)} clock hours; periods with a full 3-hour lead-in (20+ markets): {len(p)}. Periods chosen after seeing the data.")
print("every reading is the median over the markets that closed in the 3 hours before the hour; per kind: the median of those hourly medians (25th to 75th)")
allm = m.copy()
print(f"  all markets, whole tape: " + ", ".join(f"{c} {allm[c].median():.2f}" for c in READ))
for kind in ("loss hours (worst fifth by net per $100)", "clean hours (best fifth by net per $100)", "other hours"):
    s = p[p["kind"] == kind]
    if not len(s):
        print(f"  {kind}: none")
        continue
    print(f"  {kind}: hours {len(s)}, bets {int(s['n'].sum())}, lost {int(s['lost'].sum())}, markets in lead-in median {s['markets'].median():.0f}")
    for c in READ:
        print(f"      {c:10s} median {s[c].median():7.3f}  ({s[c].quantile(0.25):7.3f} to {s[c].quantile(0.75):7.3f})")
print("--- the hours themselves (a label for reading, not a rule)")
for _, r in p.sort_values("hour").iterrows():
    print(f"  {pd.to_datetime(r['hour'] * 3600, unit='s'):%m-%d %H}:00 UTC  {r['kind'][:5]:5s} bets {int(r['n']):3d} lost {int(r['lost']):2d} net/100 {g.loc[r['hour'],'per100']:+6.1f} | unit {r['unit']:.2f} flips {r['flips']:.1f} contested {r['contested']:.2f} decisive {r['decisive']:.3f} eff {r['eff']:.2f} range {r['range']:.2f}")
print("DONE")
