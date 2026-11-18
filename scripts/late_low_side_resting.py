"""Low sides in the last four minutes with RESTING orders on both legs, judged on Kalshi's trade prints (2026-11-18, user: "Yes, test it", after scripts/late_low_side_trigger.py found the play near fair less two spreads when it crosses in and out)

One question on the same moments (every 10 s of the last four minutes, a side's ask at or under 0.30, the favourite changed in the five minutes before): a bid rests at the side's own BID; if it fills, a sale rests 20 cents higher until the close; no fee on either leg. Does earning the spread instead of paying it turn the play positive?
Fills come from the trades Kalshi printed (tape flow_1s: per second, contracts bought by YES takers and by NO takers, and their average YES price), the way paperfill.py judges a resting bid, and from the quotes:
  kind fill    a taker traded against our side AT our price or better in a later second, or the quote went through it (first in the queue: the best case)
  strict fill  a taker traded THROUGH our price, or the quote went through it (last in the queue: the worst case)
The truth for a real order lies between the two. A second's trades are one average price, so a second with trades at several prices is blurred.
The bid's life: until the close, or an eighth of the time left (30 s at four minutes, scaling down; stated before the run). A bid that never fills is no bet. A filled bet whose sale never fills is counted at its settlement.
Tape: data/tape_archive/quotes and data/tape_archive/flow_1s (the archive bucket's tape/flow_1s days), 2026-11-09 on. Kalshi's own data only. 0.30, 20 cents, four and five minutes are the user's numbers.
Run on the Mac: .venv/bin/python scripts/late_low_side_resting.py > studies/results/<date>-late-low-side-resting.txt"""
import csv
import glob
import math

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

BUY, GAIN, LAST, BACK, STEP = 0.30, 0.20, 240, 300.0, 10
E = 1e-9
res = {r[0]: (float(r[1]), r[2]) for r in csv.reader(open("data/tape_archive/windows.csv")) if len(r) >= 3 and r[2] in ("yes", "no")}
rows = []
days = sorted(glob.glob("data/tape_archive/quotes/day=*/part-0.parquet"))


def first_after(times, mask, after, before):
    idx = np.flatnonzero(mask & (times > after) & (times < before))
    return float(times[idx[0]]) if len(idx) else math.inf


for f in days:
    day = f.split("day=")[1][:10]
    try:
        fl = pq.read_table(f.replace("/quotes/", "/flow_1s/"), columns=["ts", "ticker", "yes_vol", "no_vol", "vwap"])
    except FileNotFoundError:
        continue
    fl = fl.filter(pc.match_substring(fl["ticker"], "15M-")).to_pandas().dropna(subset=["vwap"]).sort_values(["ticker", "ts"])
    flow = {tk: (g["ts"].to_numpy(dtype=float), g["yes_vol"].to_numpy(), g["no_vol"].to_numpy(), g["vwap"].to_numpy()) for tk, g in fl.groupby("ticker", sort=False)}
    t = pq.read_table(f, columns=["ts", "ticker", "bid", "ask"])
    t = t.filter(pc.match_substring(t["ticker"], "15M-"))
    df = t.to_pandas().sort_values(["ticker", "ts"])
    for ticker, g in df.groupby("ticker", sort=False):
        if ticker not in res or ticker not in flow or len(g) < 60:
            continue
        close, result = res[ticker]
        ts = g["ts"].to_numpy()
        yb, ya = g["bid"].to_numpy(), g["ask"].to_numpy()
        ok = (yb > 0) & (ya > 0) & (ya <= 1) & (ya >= yb) & (ts < close)
        ts, yb, ya = ts[ok], yb[ok], ya[ok]
        if len(ts) < 60 or ts[0] > close - 600:
            continue
        fav = pd.Series(np.where(yb >= 0.50, 1.0, np.where(ya <= 0.50, -1.0, np.nan))).ffill().to_numpy()
        change = np.zeros(len(ts), dtype=bool)
        change[1:] = (fav[1:] != fav[:-1]) & ~np.isnan(fav[1:]) & ~np.isnan(fav[:-1])
        cts = ts[change]
        fts, yv, nv, vw = flow[ticker]
        for left in range(LAST, 0, -STEP):
            now = close - left
            i = int(np.searchsorted(ts, now, side="right") - 1)
            if i < 20 or now - ts[i] > 30:
                continue
            n_cross = int(np.sum((cts > now - BACK) & (cts <= now)))
            if n_cross < 1:
                continue
            for side, b, a, sp, into_bid, into_ask in (("yes", yb, ya, vw, nv, yv), ("no", 1.0 - ya, 1.0 - yb, 1.0 - vw, yv, nv)):
                ask, p = float(a[i]), float(b[i])
                if not (0.02 < ask <= BUY) or p < 0.02:
                    continue
                x = p + GAIN
                row = {"day": day, "ticker": ticker, "left": left, "buy": p, "spread": ask - p, "changes": n_cross, "paid": result == side}
                for life_name, until in (("close", close), ("eighth", now + left / 8.0)):
                    for kind in ("kind", "strict"):
                        through = first_after(ts, a <= p + E, now, until)
                        prints = first_after(fts, (into_bid > 0) & ((sp <= p + E) if kind == "kind" else (sp < p - E)), math.floor(now), until)
                        tf = min(through, prints)
                        key = f"{life_name}_{kind}"
                        if tf == math.inf:
                            row[key] = np.nan  # no bet
                            continue
                        sold = min(first_after(ts, b >= x - E, tf, close + 1), first_after(fts, (into_ask > 0) & ((sp >= x - E) if kind == "kind" else (sp > x + E)), math.floor(tf), close + 1))
                        row[key] = GAIN if sold < math.inf else float(result == side) - p
                rows.append(row)

df = pd.DataFrame(rows)
ndays = df["day"].nunique()
KEYS = (("close_kind", "bid rests to the close, kind fill"), ("close_strict", "bid rests to the close, strict fill"), ("eighth_kind", "bid rests an eighth of the time left, kind"), ("eighth_strict", "bid rests an eighth of the time left, strict"))


def show(label, sel, key):
    got = sel[sel[key].notna()]
    if len(got) < 30:
        print(f"  {label:44s} bids {len(sel):6d} | filled {len(got):6d}")
        return
    per_day = got.groupby("day")[key].sum()
    sold = (got[key] == GAIN).mean()
    need = (got["buy"] / (got["buy"] + GAIN)).mean()
    print(f"  {label:44s} bids {len(sel):6d} | filled {len(got):6d} ({100.0 * len(got) / len(sel):4.1f}%) on {got['ticker'].nunique():5d} markets | avg buy {got['buy'].mean():.3f} | sale filled {100.0 * sold:5.1f}% (needs about {100.0 * need:.0f}%) | net {got[key].sum():+9.2f} ({got[key].mean():+.3f} a filled bet) | days positive {int((per_day > 0).sum())} of {ndays}")


print(f"window: {df['day'].min()} to {df['day'].max()}; {len(df)} resting bids on {df['ticker'].nunique()} markets (every 10 s of the last 4 minutes, a side's ask at or under {BUY:.2f}, the favourite changed in the 5 minutes before). The bid rests at the side's bid; on a fill the sale rests {GAIN:.2f} higher; no fee; one contract a bid. Average spread {df['spread'].mean():.3f}.")
for key, name in KEYS:
    show(name, df, key)
for key, name in KEYS[:2] + KEYS[2:]:
    print(f"=== {name}")
    print("  --- by the buy price")
    for lo, hi in ((0.0, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.30)):
        show(f"buy {lo:.2f} to {hi:.2f}", df[(df["buy"] > lo) & (df["buy"] <= hi)], key)
    print("  --- by the time left when the bid goes in")
    for lo, hi in ((180, 240), (120, 180), (60, 120), (0, 60)):
        show(f"{lo} to {hi} s left", df[(df["left"] > lo) & (df["left"] <= hi)], key)
    print("  --- by the changes of favourite in the five minutes before")
    for lo, hi, nm in ((1, 2, "1 or 2"), (3, 4, "3 or 4"), (5, 10 ** 6, "5 or more")):
        show(f"changes {nm}", df[(df["changes"] >= lo) & (df["changes"] <= hi)], key)
    print("  --- by the spread when the bid goes in")
    for lo, hi, nm in ((0.0, 0.0105, "1 cent"), (0.0105, 0.0205, "2 cents"), (0.0205, 0.0405, "3 or 4 cents"), (0.0405, 1.0, "5 cents or more")):
        show(f"spread {nm}", df[(df["spread"] > lo) & (df["spread"] <= hi)], key)
print("DONE")
