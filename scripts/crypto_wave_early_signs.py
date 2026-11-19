"""Can a wave of same-way crypto bets be seen coming? (2026-11-18, user: "We need to detect these waves earlier we have enough tape on them")

One question on the same bets: a crypto side bought at its ask when it is first quoted from 0.60 to 0.85 at a 30 s mark with 3 to 13 minutes left, one per market and side, held to settlement, taker fee on the buy (the lane's 60-85 band; the risk score and the ladder are NOT replayed). For every such bet, what the OTHER crypto markets of the same 15-minute window were doing at that mark, in the market's own typical minutes (rms one-second mid move times sqrt(60), from the quotes before the mark):
  rising     how many other markets have the SAME way (yes or no) up by at least one typical minute of its own over the last 60 s: a wave that has begun
  falling    how many other markets have the SAME way down by at least one typical minute over the last 60 s (added after the first run showed `rising` alone does not separate: a wave is a split between rising and falling markets, not only a count of risers)
  wave       how many other markets have a bet in the same way within 60 s of this one (the lane's own measure; known only afterwards for the earlier ones)
  order      this bet's place among its wave's bets in time (1st, 2nd, 3rd, 4th or later)
Reported by each reading: bets, win rate against the average ask, net per $100 staked, bets lost. `rising` is the reading that is known when the bet is placed, so it is the candidate early sign. No coin is named in a rule: markets are counted, not named. Settings chosen after seeing the results are said to be so.
Tape: data/tape_archive/quotes, 2026-11-09 to the last archived day, results windows.csv.
Run on the Mac: .venv/bin/python scripts/crypto_wave_early_signs.py > studies/results/<date>-crypto-wave-early-signs.txt"""
import csv
import glob
import math
from collections import defaultdict

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

FEE = 0.07
LO, HI = 0.60, 0.85
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M")
MARKS = list(range(780, 170, -30))
res = {r[0]: (float(r[1]), r[2]) for r in csv.reader(open("data/tape_archive/windows.csv")) if len(r) >= 3 and r[2] in ("yes", "no")}
rows = []
days = sorted(glob.glob("data/tape_archive/quotes/day=*/part-0.parquet"))
for f in days:
    day = f.split("day=")[1][:10]
    t = pq.read_table(f, columns=["ts", "ticker", "bid", "ask"])
    t = t.filter(pc.match_substring(t["ticker"], "15M-"))
    df = t.to_pandas()
    df = df[df["ticker"].str.split("-").str[0].isin(SERIES)].sort_values(["ticker", "ts"])
    marks = defaultdict(dict)  # close -> ticker -> {left: {side: (ask, move60_in_units)}}
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
        d = np.where(have[1:] & have[:-1], np.diff(msec), np.nan)
        d2 = d * d
        for left in MARKS:
            now = close - left
            k = 900 - left
            i = int(np.searchsorted(ts, now, side="right") - 1)
            if i < 20 or now - ts[i] > 30 or k < 300:
                continue
            seen = d2[:k][~np.isnan(d2[:k])]
            if len(seen) < 120:
                continue
            unit = float(np.sqrt(np.mean(seen)) * math.sqrt(60.0))
            if unit <= 0 or np.isnan(msec[k]) or np.isnan(msec[k - 60]):
                continue
            mv = (msec[k] - msec[k - 60]) / unit
            marks[close].setdefault(ticker, {})[left] = {"yes": (float(ya[i]), mv), "no": (1.0 - float(yb[i]), -mv), "unit": unit, "result": result}
    for close, tk in marks.items():
        entered = {}  # (ticker, side) -> left of first entry
        for ticker, byleft in tk.items():
            for left in MARKS:
                if left not in byleft:
                    continue
                for side in ("yes", "no"):
                    a = byleft[left][side][0]
                    if (ticker, side) not in entered and LO <= a <= HI:
                        entered[(ticker, side)] = left
        for (ticker, side), left in entered.items():
            a, mv = tk[ticker][left][side]
            result = tk[ticker][left]["result"]
            rising = sum(1 for o, byleft in tk.items() if o != ticker and left in byleft and byleft[left][side][1] >= 1.0)
            falling = sum(1 for o, byleft in tk.items() if o != ticker and left in byleft and byleft[left][side][1] <= -1.0)
            wave = sum(1 for (o, s2), l2 in entered.items() if o != ticker and s2 == side and abs(l2 - left) <= 60)
            order = 1 + sum(1 for (o, s2), l2 in entered.items() if o != ticker and s2 == side and abs(l2 - left) <= 60 and l2 > left)
            won = result == side
            rows.append({"day": day, "close": close, "ask": a, "left": float(left), "rising": rising, "falling": falling, "wave": wave, "order": min(order, 4), "won": won,
                         "net": (1.0 if won else 0.0) - a - FEE * a * (1.0 - a)})
df = pd.DataFrame(rows)


def show(label, sel):
    if len(sel) < 30:
        print(f"  {label:30s} bets {len(sel):5d}")
        return
    per_day = sel.groupby("day")["net"].sum()
    stake = (sel["ask"] + FEE * sel["ask"] * (1.0 - sel["ask"])).sum()
    print(f"  {label:30s} bets {len(sel):5d} | win {100.0 * sel['won'].mean():5.1f}% vs avg ask {100.0 * sel['ask'].mean():5.1f}c | lost {int((~sel['won']).sum()):4d} | net {sel['net'].sum():+8.2f} ({100.0 * sel['net'].sum() / stake:+6.1f} per $100 staked) | days positive {int((per_day > 0).sum())} of {sel['day'].nunique()}")


print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {len(df)} sides first quoted at {LO:.2f} to {HI:.2f} at a 30 s mark with 3 to 13 min left, one a market and side, held to settlement, fee in on the buy, the risk score and ladder not replayed")
show("all", df)
print("--- by how many other markets are already RISING the same way (known at the buy)")
for lo, hi, nm in ((0, 0, "0"), (1, 1, "1"), (2, 2, "2"), (3, 4, "3 or 4"), (5, 6, "5 or 6"), (7, 99, "7 or more")):
    show(f"rising {nm}", df[(df["rising"] >= lo) & (df["rising"] <= hi)])
print("--- by how many other markets are FALLING the same way at the buy (known at the buy)")
for lo, hi, nm in ((0, 0, "0"), (1, 1, "1"), (2, 3, "2 or 3"), (4, 99, "4 or more")):
    show(f"falling {nm}", df[(df["falling"] >= lo) & (df["falling"] <= hi)])
print("--- the split: rising and falling together (known at the buy)")
for rl, rh, rn in ((0, 1, "rising 0 to 1"), (2, 4, "rising 2 to 4"), (5, 99, "rising 5 or more")):
    for fl, fh, fn in ((0, 1, "falling 0 to 1"), (2, 99, "falling 2 or more")):
        show(f"{rn}, {fn}", df[(df["rising"] >= rl) & (df["rising"] <= rh) & (df["falling"] >= fl) & (df["falling"] <= fh)])
print("--- by the wave around the bet (known afterwards for the earlier ones)")
for lo, hi, nm in ((0, 0, "alone"), (1, 2, "1 or 2 others"), (3, 5, "3 to 5 others"), (6, 99, "6 or more others")):
    show(f"wave {nm}", df[(df["wave"] >= lo) & (df["wave"] <= hi)])
print("--- by this bet's order among its wave (waves of 3 or more others)")
big = df[df["wave"] >= 3]
for o in (1, 2, 3, 4):
    show(f"order {o}{'+' if o == 4 else ''}", big[big["order"] == o])
print("--- rising by the time left (known at the buy)")
for part, test in (("10 to 13 min left", lambda x: x["left"] >= 600), ("5 to 10 min left", lambda x: (x["left"] >= 300) & (x["left"] < 600)), ("3 to 5 min left", lambda x: x["left"] < 300)):
    for lo, hi, nm in ((0, 1, "rising 0 to 1"), (2, 99, "rising 2 or more")):
        sel = df[test(df) & (df["rising"] >= lo) & (df["rising"] <= hi)]
        show(f"{part}, {nm}", sel)
print("DONE")
