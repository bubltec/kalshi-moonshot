"""Last four minutes: which markets gave 20-cent rises from a low side? (2026-11-18, user: "i just want to see if we can make 20c or more increments on those markets. which ones showed that behavior")

A count, not a bet: in the last four minutes of each 15-minute market, a rise is a side's ASK at or under 0.30 (the low) followed by the same side's BID at least 0.20 over that low before the close: a buy and a sale that were both quoted. After a rise the search starts again, so a market can give several (either side).
Markets are described by what the curve was doing AT the four-minute mark, so the reading is known before any rise:
  changes   changes of favourite in the five minutes before the mark (YES bid at or over 0.50 makes YES the favourite, YES ask at or under 0.50 makes NO; in between the last one stands)
  low side  the cheaper side's ask at the mark
  swing     the mid's high less low over those five minutes, in the market's typical minutes (rms one-second move of the mid so far, times sqrt(60))
  typical minute, in cents (fifths of all markets: a study's cut)
Tape: archived Kalshi quotes (data/tape_archive/quotes, days from 2026-11-09), markets with a result. Kalshi's own quotes only. 0.30, 0.20 and four and five minutes are the user's numbers.
Run on the Mac: .venv/bin/python scripts/late_low_side_increments.py > studies/results/<date>-late-low-side-increments.txt"""
import csv
import glob
import math

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

LOW, GAIN, LAST, BACK = 0.30, 0.20, 240.0, 300.0
res = {r[0]: (float(r[1]), r[2]) for r in csv.reader(open("data/tape_archive/windows.csv")) if len(r) >= 3 and r[2] in ("yes", "no")}
markets, rises = [], []
days = sorted(glob.glob("data/tape_archive/quotes/day=*/part-0.parquet"))
for f in days:
    day = f.split("day=")[1][:10]
    t = pq.read_table(f, columns=["ts", "ticker", "bid", "ask"])
    t = t.filter(pc.match_substring(t["ticker"], "15M-"))
    df = t.to_pandas().sort_values(["ticker", "ts"])
    for ticker, g in df.groupby("ticker", sort=False):
        if ticker not in res or len(g) < 60:
            continue
        close, _ = res[ticker]
        ts = g["ts"].to_numpy()
        yb, ya = g["bid"].to_numpy(), g["ask"].to_numpy()
        ok = (yb > 0) & (ya > 0) & (ya <= 1) & (ya >= yb) & (ts < close)
        ts, yb, ya = ts[ok], yb[ok], ya[ok]
        mark = close - LAST
        i0 = int(np.searchsorted(ts, mark, side="right"))  # first quote inside the last four minutes
        if i0 < 60 or len(ts) - i0 < 10 or ts[0] > close - 600:
            continue
        mid = (yb + ya) / 2.0
        fav = pd.Series(np.where(yb >= 0.50, 1.0, np.where(ya <= 0.50, -1.0, np.nan))).ffill().to_numpy()
        change = np.zeros(len(ts), dtype=bool)
        change[1:] = (fav[1:] != fav[:-1]) & ~np.isnan(fav[1:]) & ~np.isnan(fav[:-1])
        changes = int(np.sum(change[(ts > mark - BACK) & (ts <= mark)]))
        sec = np.floor(ts[:i0]).astype(np.int64)
        d = np.diff(pd.Series(mid[:i0], index=sec).groupby(level=0).last().to_numpy())
        if len(d) < 120:
            continue
        unit = float(np.sqrt(np.mean(d * d)) * math.sqrt(60.0))
        if unit <= 0:
            continue
        w = mid[(ts > mark - BACK) & (ts <= mark)]
        n = 0
        for side, b, a in (("yes", yb, ya), ("no", 1.0 - ya, 1.0 - yb)):
            low, low_t = None, 0.0
            for j in range(i0, len(ts)):
                if low is not None and b[j] >= low + GAIN - 1e-9:
                    rises.append({"ticker": ticker, "low": low, "left": close - low_t, "took": ts[j] - low_t, "changes": changes})
                    n += 1
                    low = None
                    continue
                if a[j] <= LOW and a[j] > 0.01 and (low is None or a[j] < low):
                    low, low_t = float(a[j]), float(ts[j])
        low_ask = float(min(ya[i0 - 1], 1.0 - yb[i0 - 1]))
        had_low = bool(np.any((ya[i0:] <= LOW) | (1.0 - yb[i0:] <= LOW)))
        markets.append({"day": day, "ticker": ticker, "changes": changes, "unit": unit, "swing": (float(w.max() - w.min()) / unit) if len(w) else 0.0, "low_ask": low_ask, "had_low": had_low, "rises": n})

m = pd.DataFrame(markets)
r = pd.DataFrame(rises)


def show(label, sel):
    if len(sel) < 20:
        print(f"  {label:38s} markets {len(sel):5d}")
        return
    low = sel[sel["had_low"]]
    print(f"  {label:38s} markets {len(sel):5d} | a side at or under 0.30 in {len(low):5d} | at least one rise {int((sel['rises'] >= 1).sum()):5d} ({100.0 * (sel['rises'] >= 1).mean():4.1f}% of markets, {100.0 * (low['rises'] >= 1).mean() if len(low) else 0:4.1f}% of those with a low side) | two or more {int((sel['rises'] >= 2).sum()):4d} ({100.0 * (sel['rises'] >= 2).mean():4.1f}%) | rises {int(sel['rises'].sum()):5d} ({sel['rises'].mean():.2f} a market)")


def fifths(col, fmt):
    edges = [-np.inf] + m[col].quantile([0.2, 0.4, 0.6, 0.8]).to_list() + [np.inf]
    for lo, hi in zip(edges[:-1], edges[1:]):
        show(f"{col} " + ("under " + fmt.format(hi) if lo == -np.inf else "over " + fmt.format(lo) if hi == np.inf else fmt.format(lo) + " to " + fmt.format(hi)), m[(m[col] > lo) & (m[col] <= hi)])


print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {len(m)} 15-minute markets with a result; {len(r)} rises (ask at or under {LOW:.2f}, then the same side's bid {GAIN:.2f} or more over it, inside the last 4 minutes).")
show("all markets", m)
print("--- by changes of favourite in the five minutes before the four-minute mark")
for lo, hi, name in ((0, 0, "none"), (1, 1, "1"), (2, 2, "2"), (3, 4, "3 or 4"), (5, 8, "5 to 8"), (9, 10 ** 6, "9 or more")):
    show(f"changes {name}", m[(m["changes"] >= lo) & (m["changes"] <= hi)])
print("--- by the cheaper side's ask at the mark")
for lo, hi in ((0.0, 0.05), (0.05, 0.15), (0.15, 0.30), (0.30, 0.40), (0.40, 0.51)):
    show(f"low side {lo:.2f} to {hi:.2f}", m[(m["low_ask"] > lo) & (m["low_ask"] <= hi)])
print("--- by the swing over those five minutes, in typical minutes (fifths)")
fifths("swing", "{:.1f}")
print("--- by the typical minute (fifths)")
fifths("unit", "{:.3f}")
print("--- changes of favourite x the cheaper side's ask at the mark")
for clo, chi, cname in ((0, 0, "no change"), (1, 2, "1 or 2"), (3, 10 ** 6, "3 or more")):
    for lo, hi in ((0.0, 0.15), (0.15, 0.30), (0.30, 0.51)):
        show(f"{cname}, low side {lo:.2f} to {hi:.2f}", m[(m["changes"] >= clo) & (m["changes"] <= chi) & (m["low_ask"] > lo) & (m["low_ask"] <= hi)])
print("--- by day (markets with a change of favourite before the mark)")
for day in sorted(m["day"].unique()):
    show(day, m[(m["day"] == day) & (m["changes"] >= 1)])
print("--- the rises themselves")
if len(r):
    print(f"  the low: median {r['low'].median():.2f} (a quarter under {r['low'].quantile(0.25):.2f}, a quarter over {r['low'].quantile(0.75):.2f}) | from the low to the sale price: median {r['took'].median():.0f} s (a quarter under {r['took'].quantile(0.25):.0f} s, a quarter over {r['took'].quantile(0.75):.0f} s)")
    for lo, hi in ((180, 240), (120, 180), (60, 120), (0, 60)):
        s = r[(r["left"] > lo) & (r["left"] <= hi)]
        print(f"  low set with {lo} to {hi} s left: {len(s):5d} rises | median low {s['low'].median():.2f} | median time to the sale price {s['took'].median():.0f} s")
print("DONE")
