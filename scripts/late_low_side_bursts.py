"""Low sides in the last four minutes: which curve conditions come before a 20-cent burst? (2026-11-18, user: "its playing short bursts on qualified markets ... we need to look at markets where the price is low (under 30c) in markets during the last 4 mins"; "maybe we can identify some conditions where these buys would be capable of making 20c profits without trying to win the market")

One question on the same moments: every 10 seconds of the last four minutes of a 15-minute market whose favourite changed at least once in the five minutes before, each side whose ask is at or under 0.30 is a moment. Bought there (a resting bid, no fee), a sale rests 20 cents higher (no fee). Did the side's BID get there before the close?
A moment that never gets there is counted at what the bet settled for (1 or 0) less the buy: it is not a hold on purpose, it is what an unsold bet becomes. No comparison with holding.
Tape: archived Kalshi quotes (data/tape_archive/quotes, days from 2026-11-09), results from windows.csv. Kalshi's own quotes only. Moments in one market are not independent, so the number of markets and the days with a positive result are shown beside each line.
A change of favourite: YES bid at or over 0.50 makes YES the favourite, YES ask at or under 0.50 makes NO; in between the last one stands.
The readings, all in the market's own typical minutes (the rms one-second move of the mid so far, times sqrt(60)):
  reach      0.20 over (typical minute x sqrt(minutes left)): under 1, an ordinary move gets there
  slope 30s  the side's move over the last 30 s
  lift       how far the side stands above its low of the last 60 s (off the trough)
  drop       how far it stands under its high of the last 60 s
  swing 60s  the side's high less low over the last 60 s
  since      seconds since the favourite last changed; changes: how many in the five minutes
Continuous readings are cut at the fifths of all moments (a study's cut, not a live rule). 0.30, 20 cents, four and five minutes are the user's numbers.
FILL=rest: the buy is a true resting bid at the side's bid, a bet only if the ask later comes down to it (the plain run buys at the ask without a fee, which a resting bid cannot do).
Run on the Mac: .venv/bin/python scripts/late_low_side_bursts.py > studies/results/<date>-late-low-side-bursts.txt"""
import csv
import glob
import math
import os

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

BUY, GAIN, LAST, BACK, STEP = 0.30, 0.20, 240, 300.0, 10
COMBO = os.environ.get("COMBO", "")
REST = os.environ.get("FILL", "") == "rest"  # the buy rests at the side's BID and is a bet only if the ask later comes down to it
res = {r[0]: (float(r[1]), r[2]) for r in csv.reader(open("data/tape_archive/windows.csv")) if len(r) >= 3 and r[2] in ("yes", "no")}
rows = []
unfilled = 0
days = sorted(glob.glob("data/tape_archive/quotes/day=*/part-0.parquet"))
for f in days:
    day = f.split("day=")[1][:10]
    t = pq.read_table(f, columns=["ts", "ticker", "bid", "ask"])
    t = t.filter(pc.match_substring(t["ticker"], "15M-"))
    df = t.to_pandas().sort_values(["ticker", "ts"])
    for ticker, g in df.groupby("ticker", sort=False):
        if ticker not in res or len(g) < 60:
            continue
        close, result = res[ticker]
        ts = g["ts"].to_numpy()
        yb, ya = g["bid"].to_numpy(), g["ask"].to_numpy()
        ok = (yb > 0) & (ya > 0) & (ya <= 1) & (ya >= yb) & (ts < close)
        ts, yb, ya = ts[ok], yb[ok], ya[ok]
        if len(ts) < 60 or ts[0] > close - 600:
            continue
        mid = (yb + ya) / 2.0
        fav = pd.Series(np.where(yb >= 0.50, 1.0, np.where(ya <= 0.50, -1.0, np.nan))).ffill().to_numpy()
        change = np.zeros(len(ts), dtype=bool)
        change[1:] = (fav[1:] != fav[:-1]) & ~np.isnan(fav[1:]) & ~np.isnan(fav[:-1])
        cts = ts[change]
        # the mid at each whole second of the market's last 14 minutes
        secs = np.arange(close - 840, close)
        at = np.searchsorted(ts, secs, side="right") - 1
        have = at >= 0
        msec = np.where(have, mid[np.maximum(at, 0)], np.nan)
        d2 = np.where(have[1:] & have[:-1], np.diff(msec) ** 2, np.nan)
        for left in range(LAST, 0, -STEP):
            now = close - left
            i = int(np.searchsorted(ts, now, side="right") - 1)
            k = 840 - left  # index of this second in secs
            if i < 20 or now - ts[i] > 30:
                continue
            n_cross = int(np.sum((cts > now - BACK) & (cts <= now)))
            if n_cross < 1:
                continue
            seen = d2[:k][~np.isnan(d2[:k])]
            if len(seen) < 120:
                continue
            unit = float(np.sqrt(np.mean(seen)) * math.sqrt(60.0))
            if unit <= 0:
                continue
            since = now - float(cts[cts <= now][-1])
            for side, b, a, sgn in (("yes", yb, ya, 1.0), ("no", 1.0 - ya, 1.0 - yb, -1.0)):
                buy = float(a[i])
                if not (0.02 < buy <= BUY):
                    continue
                m = msec if sgn > 0 else 1.0 - msec
                w60, w30 = m[k - 60:k + 1], m[k - 30]
                if np.isnan(w60).any():
                    continue
                spread = buy - float(b[i])
                start = i + 1
                if REST:
                    buy = float(b[i])
                    got = np.flatnonzero(a[i + 1:] <= buy + 1e-9)
                    if buy <= 0.02 or not len(got):
                        unfilled += 1
                        continue
                    start = i + 1 + int(got[0]) + 1
                later = b[start:]
                hit = np.flatnonzero(later >= buy + GAIN - 1e-9)
                rows.append({"day": day, "ticker": ticker, "left": left, "buy": buy, "changes": n_cross, "since": since, "unit": unit,
                             "reach": GAIN / (unit * math.sqrt(left / 60.0)), "slope30": (m[k] - w30) / unit, "lift": (m[k] - w60.min()) / unit,
                             "drop": (w60.max() - m[k]) / unit, "swing": (w60.max() - w60.min()) / unit,
                             "hit": bool(len(hit)), "wait": float(ts[start + hit[0]] - now) if len(hit) else np.nan, "paid": result == side, "spread": spread})

df = pd.DataFrame(rows)
df["net"] = np.where(df["hit"], GAIN, df["paid"].astype(float) - df["buy"])
ndays = df["day"].nunique()


def show(label, sel):
    if len(sel) < 30:
        print(f"  {label:34s} moments {len(sel):6d}")
        return
    per_day = sel.groupby("day")["net"].sum()
    need = (sel["buy"] / (sel["buy"] + GAIN)).mean()
    print(f"  {label:34s} moments {len(sel):6d} on {sel['ticker'].nunique():5d} markets | avg buy {sel['buy'].mean():.3f} | burst {100.0 * sel['hit'].mean():5.1f}% (needs about {100.0 * need:.0f}%) | net {sel['net'].sum():+9.2f} ({sel['net'].mean():+.3f} each) | days positive {int((per_day > 0).sum())} of {ndays} | median wait {sel.loc[sel['hit'], 'wait'].median():.0f} s")


def fifths(col, fmt="{:.2f}"):
    cuts = df[col].quantile([0.2, 0.4, 0.6, 0.8]).to_list()
    edges = [-np.inf] + cuts + [np.inf]
    for lo, hi in zip(edges[:-1], edges[1:]):
        name = f"{col} " + ("under " + fmt.format(hi) if lo == -np.inf else "over " + fmt.format(lo) if hi == np.inf else fmt.format(lo) + " to " + fmt.format(hi))
        show(name, df[(df[col] > lo) & (df[col] <= hi)])


print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {len(df)} moments on {df['ticker'].nunique()} markets (every 10 s of the last 4 minutes, a side's ask at or under {BUY:.2f}, the favourite changed in the 5 minutes before). Bought at the ask, sale {GAIN:.2f} higher, no fee on either leg; one contract a moment; an unsold bet counted at its settlement. Average spread at the moment {df['spread'].mean():.3f}." + (f" FILL=rest: the buy rests at the side's bid and counts only if the ask later came down to it ({unfilled} moments never filled and are left out); the sale is looked for after the fill." if REST else ""))
show("all", df)
print("--- by the buy price")
for lo, hi in ((0.02, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.25), (0.25, 0.30)):
    show(f"buy {lo:.2f} to {hi:.2f}", df[(df["buy"] > lo) & (df["buy"] <= hi)])
print("--- by the time left")
for lo, hi in ((180, 240), (120, 180), (60, 120), (0, 60)):
    show(f"{lo} to {hi} s left", df[(df["left"] > lo) & (df["left"] <= hi)])
print("--- by the changes of favourite in the five minutes before")
for lo, hi, name in ((1, 1, "1"), (2, 2, "2"), (3, 4, "3 or 4"), (5, 8, "5 to 8"), (9, 10 ** 6, "9 or more")):
    show(f"changes {name}", df[(df["changes"] >= lo) & (df["changes"] <= hi)])
print("--- by the seconds since the favourite last changed")
for lo, hi in ((0, 15), (15, 30), (30, 60), (60, 120), (120, 300)):
    show(f"since {lo} to {hi} s", df[(df["since"] > lo) & (df["since"] <= hi)])
for col in ("reach", "slope30", "lift", "drop", "swing"):
    print(f"--- by {col} (fifths of all moments)")
    fifths(col)
if COMBO:
    print(f"--- chosen AFTER seeing the lines above: {COMBO}")
    sel = df.query(COMBO)
    show("the combination", sel)
    for day in sorted(sel["day"].unique()):
        show(day, sel[sel["day"] == day])
    for lo, hi in ((0.02, 0.15), (0.15, 0.25), (0.25, 0.30)):
        show(f"  buy {lo:.2f} to {hi:.2f}", sel[(sel["buy"] > lo) & (sel["buy"] <= hi)])
print("DONE")
