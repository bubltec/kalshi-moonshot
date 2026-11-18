"""Crypto sides priced 0.65 to 0.78: do they pay more than their price, and in which curve shapes? (2026-11-18, user: "i think i want to go live with crypto_risk_score, but how can we get it to buy from about 65+?")

One question on the same bets: a crypto side bought at its ask when it is first quoted from 0.65 to 0.85 at a 30 s mark with 3 to 13 minutes left, one per market and side, held to settlement, taker fee on the buy. The lane buys almost only at 0.80 to 0.85 today (133 of 204 bets on 11-18), because its ladder step values the cheaper sides under their price; this asks whether the 0.65 to 0.78 sides pay anyway. The 0.80 to 0.85 band is the comparison. Bought at the ask is the kindest fill for a taker, and held to settlement leaves out the lane's own profit take, so a result here is the floor of what a bet on that side is worth, not the lane's result. The risk score and the ladder are NOT applied (neither can be replayed from the archive), so this is the whole band, not what the lane would pick inside it.
Readings at the mark are the commodity study's (Kalshi's own quotes only, in the market's own typical minutes): flips, range5, eff5, drop180, slope60, unit, left. Continuous readings are cut at the fifths of the bets studied (a study's cut, not a live rule). Per cut: bets, win rate against the average ask, net per $100 staked with the fee, bets lost, days positive. Settings chosen after seeing the results are said to be so.
Tape: data/tape_archive/quotes, 2026-11-09 to the last archived day, results windows.csv. The crypto series are the lane's own list.
Run on the Mac: .venv/bin/python scripts/crypto_midprice_by_shape.py > studies/results/<date>-crypto-midprice-by-shape.txt"""
import csv
import glob
import math

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

FEE = 0.07
LO, HI = 0.65, 0.85
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M")
res = {r[0]: (float(r[1]), r[2]) for r in csv.reader(open("data/tape_archive/windows.csv")) if len(r) >= 3 and r[2] in ("yes", "no")}
rows = []
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
        bsec = {"yes": np.where(have, yb[np.maximum(at, 0)], np.nan), "no": np.where(have, 1.0 - ya[np.maximum(at, 0)], np.nan)}
        d = np.where(have[1:] & have[:-1], np.diff(msec), np.nan)
        d2 = d * d
        taken = set()
        for left in range(780, 170, -30):
            now = close - left
            k = 900 - left
            i = int(np.searchsorted(ts, now, side="right") - 1)
            if i < 20 or now - ts[i] > 30 or k < 300:
                continue
            seen = d2[:k][~np.isnan(d2[:k])]
            if len(seen) < 120:
                continue
            unit = float(np.sqrt(np.mean(seen)) * math.sqrt(60.0))
            if unit <= 0:
                continue
            flips = int(np.sum(cts <= now))
            for side, a in (("yes", ya[i]), ("no", 1.0 - yb[i])):
                if side in taken or not (LO <= a <= HI):
                    continue
                taken.add(side)
                m = msec if side == "yes" else 1.0 - msec
                w5 = m[k - 300:k + 1]
                if np.isnan(w5).any():
                    continue
                moves = np.abs(np.diff(w5)).sum()
                b = bsec[side]
                hi180 = np.nanmax(b[k - 180:k + 1])
                won = result == side
                rows.append({"day": day, "ticker": ticker, "series": ticker.split("-")[0], "ask": float(a), "left": float(left), "flips": float(flips), "unit": unit * 100.0,
                             "range5": (w5.max() - w5.min()) / unit, "eff5": (abs(w5[-1] - w5[0]) / moves) if moves > 0 else 1.0,
                             "drop180": (hi180 - b[k]) / unit, "slope60": (m[k] - m[k - 60]) / unit,
                             "won": won, "net": (1.0 if won else 0.0) - float(a) - FEE * float(a) * (1.0 - float(a))})

df = pd.DataFrame(rows)
ndays = df["day"].nunique()


def show(label, sel):
    if len(sel) < 40:
        print(f"  {label:34s} bets {len(sel):5d}")
        return
    per_day = sel.groupby("day")["net"].sum()
    stake = sel["ask"].sum()
    print(f"  {label:34s} bets {len(sel):5d} | win {100.0 * sel['won'].mean():5.1f}% vs avg ask {100.0 * sel['ask'].mean():5.1f}c | lost {int((~sel['won']).sum()):4d} | net {sel['net'].sum():+8.2f} ({100.0 * sel['net'].sum() / stake:+5.1f} per $100 staked) | days positive {int((per_day > 0).sum())} of {ndays}")


def fifths(col, fmt="{:.2f}"):
    edges = [-np.inf] + df[col].quantile([0.2, 0.4, 0.6, 0.8]).to_list() + [np.inf]
    for lo, hi in zip(edges[:-1], edges[1:]):
        name = f"{col} " + ("under " + fmt.format(hi) if lo == -np.inf else "over " + fmt.format(lo) if hi == np.inf else fmt.format(lo) + " to " + fmt.format(hi))
        show(name, df[(df[col] > lo) & (df[col] <= hi)])


print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {len(df)} favourites on {df['ticker'].nunique()} crypto markets (first quote at {LO:.2f} to {HI:.2f} at a 30 s mark with 3 to 13 min left, one a market and side), held to settlement, taker fee on the buy, one contract each.")
show("all", df)
print("--- by the ask")
for lo, hi in ((0.65, 0.70), (0.70, 0.75), (0.75, 0.78), (0.78, 0.80), (0.80, 0.85001)):
    show(f"ask {lo:.2f} to {hi:.2f}", df[(df["ask"] >= lo) & (df["ask"] < hi)])
print("--- by flips (changes of favourite so far)")
for lo, hi, nm in ((0, 0, "0"), (1, 1, "1"), (2, 3, "2 or 3"), (4, 6, "4 to 6"), (7, 10 ** 6, "7 or more")):
    show(f"flips {nm}", df[(df["flips"] >= lo) & (df["flips"] <= hi)])
print("=== the shapes below are for asks 0.65 to 0.78 only (the band the lane does not buy); cuts are fifths of that band")
df = df[df["ask"] < 0.78].copy()
for col in ("range5", "eff5", "drop180", "slope60", "unit", "left"):
    print(f"--- by {col} (fifths of all bets)")
    fifths(col, "{:.2f}" if col != "left" else "{:.0f}")
print("--- by series (a label, not a rule)")
for ser in SERIES:
    show(ser, df[df["series"] == ser])
print("DONE")
