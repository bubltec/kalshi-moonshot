"""Low sides in the last four minutes: what is the best sale target? (2026-11-18, user: "what if we aren't selling for 20c? whats the best profit?")

One question on the same bets as scripts/late_low_side_resting.py (every 10 s of the last four minutes, a side's ask at or under 0.30, the favourite changed in the five minutes before; a bid rests at the side's bid for an eighth of the time left; on a fill a sale rests TARGET cents higher until the close, no fee; an unsold bet is counted at its settlement): the net for each target from 1 cent to 50, and for no sale at all.
Both fill assumptions for both legs: kind (a trade print AT our price fills us: first in the queue) and strict (only a print THROUGH our price or the quote through it: last in the queue). A small target lives on the difference, so the truth for it lies between the two lines.
The targets are a sweep run AFTER the 20-cent result was seen: the best line is the best of eleven on these eight days and would have to hold on later days before it means anything.
Tape: data/tape_archive/quotes and flow_1s, 2026-11-09 on. Kalshi's own data only.
Run on the Mac: .venv/bin/python scripts/late_low_side_targets.py > studies/results/<date>-late-low-side-targets.txt"""

import csv
import glob
import math

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

BUY, LAST, BACK, STEP = 0.30, 240, 300.0, 10
TARGETS = (0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30, 0.50)
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
                row = {"day": day, "ticker": ticker, "left": left, "buy": p, "spread": ask - p, "changes": n_cross}
                until = now + left / 8.0
                for kind in ("kind", "strict"):
                    tf = min(first_after(ts, a <= p + E, now, until), first_after(fts, (into_bid > 0) & ((sp <= p + E) if kind == "kind" else (sp < p - E)), math.floor(now), until))
                    if tf == math.inf:
                        continue
                    row[f"{kind}_hold"] = float(result == side) - p
                    for g_ in TARGETS:
                        x = p + g_
                        sold = min(first_after(ts, b >= x - E, tf, close + 1), first_after(fts, (into_ask > 0) & ((sp >= x - E) if kind == "kind" else (sp > x + E)), math.floor(tf), close + 1))
                        row[f"{kind}_{g_}"] = g_ if sold < math.inf else float(result == side) - p
                rows.append(row)

df = pd.DataFrame(rows)
ndays = df["day"].nunique()


def show(label, sel, key, g_=None):
    got = sel[sel[key].notna()]
    if len(got) < 30:
        print(f"  {label:30s} bets {len(got):6d}")
        return
    per_day = got.groupby("day")[key].sum()
    sold = f"sale filled {100.0 * (got[key] == g_).mean():5.1f}% (needs about {100.0 * (got['buy'] / (got['buy'] + g_)).mean():3.0f}%)" if g_ else f"paid {100.0 * (got[key] > 0).mean():5.1f}%" + " " * 21
    print(f"  {label:30s} bets {len(got):6d} | avg buy {got['buy'].mean():.3f} | {sold} | net {got[key].sum():+9.2f} ({got[key].mean():+.4f} a bet, {100.0 * got[key].sum() / got['buy'].sum():+5.1f}% of the money in) | days positive {int((per_day > 0).sum())} of {ndays}")


def table(sel, kind):
    for g_ in TARGETS:
        show(f"sale {round(100 * g_)} cents higher", sel, f"{kind}_{g_}", g_)
    show("no sale (settles)", sel, f"{kind}_hold")


print(f"window: {df['day'].min()} to {df['day'].max()}; {len(df)} resting bids on {df['ticker'].nunique()} markets. One contract a bet, no fee on either leg. Average spread {df['spread'].mean():.3f}.")
for kind, name in (("kind", "kind fill (first in the queue)"), ("strict", "strict fill (last in the queue)")):
    print(f"=== {name}: all bets")
    table(df, kind)
for kind, name in (("kind", "kind fill"), ("strict", "strict fill")):
    for lo, hi in ((0.0, 0.10), (0.10, 0.20), (0.20, 0.30)):
        print(f"=== {name}: buy {lo:.2f} to {hi:.2f}")
        table(df[(df["buy"] > lo) & (df["buy"] <= hi)], kind)
    for lo, hi, nm in ((0.0, 0.0105, "1 cent"), (0.0105, 1.0, "2 cents or more")):
        print(f"=== {name}: spread {nm} when the bid goes in")
        table(df[(df["spread"] > lo) & (df["spread"] <= hi)], kind)
print("DONE")
