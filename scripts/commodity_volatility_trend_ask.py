"""When the commodity market's volatility was high, what price did we buy at, and was it a trend or calm? (2026-11-19, user: "i think volatility does have an impact on the win rate, and the confidence we can buy at. when volatility was high, what % did we buy at and was it during a trend or calm?")
The same bets as scripts/commodity_favourite_by_shape.py (a commodity side first quoted from 0.60 to 0.92 at a 30 s mark with 3 to 13 min left, one a market and side, held to settlement, taker fee, one contract each). Every reading is taken BEFORE the buy, from the market's own quotes:
  volatility   the market's typical minute so far (rms one-second mid move times sqrt(60)), cents; cut in thirds of the bets studied
  trend        the side's path efficiency over the last five minutes (net move over the sum of one-second moves): 1 = a straight line, near 0 = going nowhere; cut at the median of the bets studied. "Trending" = efficiency over the median, "calm or choppy" = at or under it
  drift        the side's mid now less 5 min ago, in typical minutes: positive = the side has been gaining
Per cell: bets, the average ask we paid (the confidence the price carries), the win rate against it, net per $100 staked, days positive of 8. Cuts are a study's, chosen after seeing the readings and said to be so. Nothing is keyed on a market name.
Run on the Mac: .venv/bin/python scripts/commodity_volatility_trend_ask.py > studies/results/<date>-commodity-volatility-trend-ask.txt"""
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
                             "drop180": (hi180 - b[k]) / unit, "slope60": (m[k] - m[k - 60]) / unit, "drift5": (m[k] - m[k - 300]) / unit,
                             "won": won, "net": (1.0 if won else 0.0) - float(a) - FEE * float(a) * (1.0 - float(a))})

df = pd.DataFrame(rows)
ndays = df["day"].nunique()


def show(label, sel):
    if len(sel) < 30:
        print(f"  {label:44s} bets {len(sel):5d}")
        return
    per_day = sel.groupby("day")["net"].sum()
    print(f"  {label:44s} bets {len(sel):5d} | avg ask {100.0 * sel['ask'].mean():5.1f}c | win {100.0 * sel['won'].mean():5.1f}% ({100.0 * (sel['won'].mean() - sel['ask'].mean()):+5.1f} vs ask) | net {sel['net'].sum():+8.2f} ({100.0 * sel['net'].sum() / sel['ask'].sum():+5.1f} per $100) | days positive {int((per_day > 0).sum())} of {ndays}")


q1, q2 = df["unit"].quantile([1 / 3, 2 / 3])
med = df["eff5"].median()
df["vol"] = np.where(df["unit"] <= q1, f"low (under {q1:.1f}c)", np.where(df["unit"] <= q2, f"middle ({q1:.1f} to {q2:.1f}c)", f"high (over {q2:.1f}c)"))
df["trend"] = np.where(df["eff5"] > med, "trending", "calm or choppy")
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {len(df)} commodity favourites; efficiency cut at its median {med:.3f}; volatility cut in thirds.")
show("all", df)
for v in (f"low (under {q1:.1f}c)", f"middle ({q1:.1f} to {q2:.1f}c)", f"high (over {q2:.1f}c)"):
    print(f"--- volatility {v}")
    s = df[df["vol"] == v]
    show("all bets", s)
    for tr in ("trending", "calm or choppy"):
        t = s[s["trend"] == tr]
        show(tr, t)
        show(f"{tr}, the side gaining (drift5 over 0)", t[t["drift5"] > 0])
        show(f"{tr}, the side losing (drift5 at or under 0)", t[t["drift5"] <= 0])
print("--- the ask we paid, by volatility (share of bets in each price band)")
for v in (f"low (under {q1:.1f}c)", f"middle ({q1:.1f} to {q2:.1f}c)", f"high (over {q2:.1f}c)"):
    s = df[df["vol"] == v]
    print(f"  {v:28s} " + "  ".join(f"{lo:.2f}-{hi:.2f}: {100.0 * ((s['ask'] >= lo) & (s['ask'] < hi)).mean():4.0f}%" for lo, hi in ((0.60, 0.70), (0.70, 0.80), (0.80, 0.86), (0.86, 0.92001))))
print("DONE")
