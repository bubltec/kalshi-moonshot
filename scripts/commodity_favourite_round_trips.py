"""Commodity favourites sold into strength before settlement: round trips against holding (2026-11-18, user: "Yes, study it", after scripts/commodity_favourite_by_shape.py found no shape that makes a favourite lose less often than its price; "making $1 on a $10 is ok, but a single loss sets us back 5-7 wins")

One question on the same bets as scripts/commodity_favourite_by_shape.py (a commodity side first quoted from 0.60 to 0.92 at a 30 s mark with 3 to 13 min left, one a market and side, bought at the ask, taker fee on the buy): the bet is sold when its bid reaches a target, and a bet not sold is held to settlement (a loser rides: there is no loss sale here). Against holding the same bets.
Targets, each tried as stated before the run:
  up k      the bid reaches the buy price plus k typical minutes of the market (k = 0.5, 1, 2, 3), the market's own units (rms one-second move of the mid so far, times sqrt(60))
  cash P    the bid reaches P: 0.95 and 0.97 (the user's "cashed out in the 90s"; structural, not fitted)
  trail k   the bet is ahead after the fees and its bid falls k typical minutes (1, 2) under its high since the buy: sold at that bid (the lane's trail)
Two sales: TAKER sells at the bid that second with the fee on the sale; RESTING rests at the target price and is filled when the bid reaches it, no fee (the best case: a trail has no resting version). The buy keeps its taker fee in both.
Reported per rule: bets, how many sold, the sold bets' win rate against what they would have been (held), net dollars for one contract, net per $100 staked, the same bets held, beat and lost to holding, days positive of 8. Then the best rule by ask band, time left and flips (changes of favourite so far). The splits and the best rule are chosen after seeing the table, and said so.
Tape: data/tape_archive/quotes, 2026-11-09 to 11-16, windows.csv. Kalshi's own quotes only.
Run on the Mac: .venv/bin/python scripts/commodity_favourite_round_trips.py > studies/results/<date>-commodity-favourite-round-trips.txt"""
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
UPS = (0.5, 1.0, 2.0, 3.0)
CASH = (0.95, 0.97)
TRAILS = (1.0, 2.0)


def fee(p):
    return FEE * p * (1.0 - p)


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
                a = float(a)
                path = bsec[side][k + 1:]  # the side's bid each second until the close
                path = path[~np.isnan(path)]
                won = result == side
                held = (1.0 if won else 0.0) - a - fee(a)
                row = {"day": day, "ticker": ticker, "ask": a, "left": float(left), "flips": float(flips), "won": won, "held": held}

                def first(mask):
                    ix = np.flatnonzero(mask)
                    return int(ix[0]) if len(ix) else None

                rules = {}
                for u in UPS:
                    tgt = a + u * unit
                    rules[f"up {u:g}"] = (first(path >= tgt - 1e-9), tgt) if tgt < 0.995 else (None, tgt)
                for p in CASH:
                    rules[f"cash {p:g}"] = (first(path >= p - 1e-9), p) if p > a else (None, p)
                for r_, (j, tgt) in rules.items():
                    row[f"{r_} taker"] = (float(path[j]) - fee(float(path[j])) - a - fee(a)) if j is not None else held
                    row[f"{r_} resting"] = (tgt - a - fee(a)) if j is not None else held
                    row[f"{r_} sold"] = j is not None
                for u in TRAILS:
                    peak, sold = a, None
                    for j, bb in enumerate(path):
                        peak = max(peak, float(bb))
                        if bb - fee(float(bb)) > a + fee(a) and bb <= peak - u * unit + 1e-9:
                            sold = j
                            break
                    row[f"trail {u:g} taker"] = (float(path[sold]) - fee(float(path[sold])) - a - fee(a)) if sold is not None else held
                    row[f"trail {u:g} sold"] = sold is not None
                rows.append(row)

df = pd.DataFrame(rows)
ndays = df["day"].nunique()
RULES = [(f"up {u:g}", ("taker", "resting")) for u in UPS] + [(f"cash {p:g}", ("taker", "resting")) for p in CASH] + [(f"trail {u:g}", ("taker",)) for u in TRAILS]


def show(label, sel, key, sold_key):
    if len(sel) < 40:
        print(f"  {label:30s} bets {len(sel):5d}")
        return
    per_day = sel.groupby("day")[key].sum()
    stake = sel["ask"].sum()
    s = sel[sel[sold_key]]
    beat = int((sel[key] > sel["held"] + 1e-9).sum())
    lost = int((sel[key] < sel["held"] - 1e-9).sum())
    print(f"  {label:30s} bets {len(sel):5d} | sold {len(s):5d} (of those {100.0 * s['won'].mean() if len(s) else 0:4.0f}% would have won) | net {sel[key].sum():+8.2f} ({100.0 * sel[key].sum() / stake:+5.1f} per $100) | held {sel['held'].sum():+8.2f} | beat holding {beat:4d}, lost to it {lost:4d} | days positive {int((per_day > 0).sum())} of {ndays}")


print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {len(df)} commodity favourites on {df['ticker'].nunique()} markets (first quote at {LO:.2f} to {HI:.2f}, 3 to 13 min left), one contract each, taker fee on the buy; a bet not sold is held to settlement.")
print(f"  {'held to settlement':30s} bets {len(df):5d} | net {df['held'].sum():+8.2f} ({100.0 * df['held'].sum() / df['ask'].sum():+5.1f} per $100) | win {100.0 * df['won'].mean():.1f}% vs avg ask {100.0 * df['ask'].mean():.1f}c")
nofee = df["held"] + df["ask"].map(fee)
print(f"  {'held, buy with NO fee (maker)':30s} bets {len(df):5d} | net {nofee.sum():+8.2f} ({100.0 * nofee.sum() / df['ask'].sum():+5.1f} per $100) | the fee on the buy is {df['ask'].map(fee).sum():.2f} of the {-df['held'].sum():.2f} lost | days positive {int((nofee.groupby(df['day']).sum() > 0).sum())} of {ndays}")
for name, kinds in RULES:
    for kind in kinds:
        show(f"{name} {kind}", df, f"{name} {kind}", f"{name} sold")
best = max(((n, k) for n, ks in RULES for k in ks), key=lambda nk: df[f"{nk[0]} {nk[1]}"].sum())
print(f"--- the best rule on all bets, chosen after seeing the table: {best[0]} {best[1]}")
key, sold = f"{best[0]} {best[1]}", f"{best[0]} sold"
print("--- by the ask")
for lo, hi in ((0.60, 0.70), (0.70, 0.80), (0.80, 0.86), (0.86, 0.92001)):
    show(f"ask {lo:.2f} to {hi:.2f}", df[(df["ask"] >= lo) & (df["ask"] < hi)], key, sold)
print("--- by the time left at the buy")
for lo, hi in ((170, 300), (300, 450), (450, 600), (600, 781)):
    show(f"{lo} to {hi} s left", df[(df["left"] >= lo) & (df["left"] < hi)], key, sold)
print("--- by flips (changes of favourite so far)")
for lo, hi, nm in ((0, 1, "0 or 1"), (2, 6, "2 to 6"), (7, 10 ** 6, "7 or more")):
    show(f"flips {nm}", df[(df["flips"] >= lo) & (df["flips"] <= hi)], key, sold)
print("DONE")
