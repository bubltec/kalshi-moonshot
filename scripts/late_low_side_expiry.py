"""Low sides in the last four minutes: resting buy, resting sale 20 cents higher, and a sale at the bid when the rise has not come by an expiration (2026-11-18, user: "exactly if it doesn't rise we should sell after an expiration")

One question on the same bets as scripts/late_low_side_resting.py (every 10 s of the last four minutes, a side's ask at or under 0.30, the favourite changed in the five minutes before; a bid rests at the side's bid for an eighth of the time left; kind fill: a trade print at our price or the quote through it; on a fill a sale rests 20 cents higher, no fee): if the sale has not filled when the expiration comes, the bet is sold at the side's bid then, with the taker fee. Against the same bets left to settle.
The expiration is a share of the time left at the fill, never fixed seconds: an eighth, a quarter, a half, three quarters (stated before the run). At four minutes those are 30, 60, 120 and 180 s.
A second line sells the expired bet with a resting sale at the side's ASK then (no fee), filled by a later print at it or the bid reaching it, else settled: the best case for getting out without the fee.
Tape: data/tape_archive/quotes and flow_1s, 2026-11-09 on. Kalshi's own data only.
Run on the Mac: .venv/bin/python scripts/late_low_side_expiry.py > studies/results/<date>-late-low-side-expiry.txt"""

import csv
import glob
import math

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

BUY, GAIN, LAST, BACK, STEP = 0.30, 0.20, 240, 300.0, 10
FEE = 0.07
SHARES = ((0.125, "an eighth"), (0.25, "a quarter"), (0.5, "a half"), (0.75, "three quarters"))
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
                until = now + left / 8.0
                tf = min(first_after(ts, a <= p + E, now, until), first_after(fts, (into_bid > 0) & (sp <= p + E), math.floor(now), until))
                if tf == math.inf:
                    continue  # no bet
                sold = min(first_after(ts, b >= x - E, tf, close + 1), first_after(fts, (into_ask > 0) & (sp >= x - E), math.floor(tf), close + 1))
                row["settle"] = GAIN if sold < math.inf else float(result == side) - p
                for share, _ in SHARES:
                    exp = tf + share * (close - tf)
                    if sold <= exp:
                        row[f"take_{share}"] = row[f"rest_{share}"] = GAIN
                        continue
                    j = int(np.searchsorted(ts, exp, side="right") - 1)
                    bid, ask2 = float(b[j]), float(a[j])
                    row[f"take_{share}"] = bid - FEE * bid * (1.0 - bid) - p
                    out = min(first_after(ts, b >= ask2 - E, exp, close + 1), first_after(fts, (into_ask > 0) & (sp >= ask2 - E), math.floor(exp), close + 1))
                    row[f"rest_{share}"] = (ask2 - p) if out < math.inf else float(result == side) - p
                rows.append(row)

df = pd.DataFrame(rows)
ndays = df["day"].nunique()


def show(label, sel, key):
    if len(sel) < 30:
        print(f"  {label:46s} bets {len(sel):6d}")
        return
    per_day = sel.groupby("day")[key].sum()
    beat = int((sel[key] > sel["settle"] + 1e-9).sum())
    lost = int((sel[key] < sel["settle"] - 1e-9).sum())
    print(f"  {label:46s} bets {len(sel):6d} on {sel['ticker'].nunique():5d} markets | avg buy {sel['buy'].mean():.3f} | rose 20c {100.0 * (sel[key] == GAIN).mean():5.1f}% | net {sel[key].sum():+9.2f} ({sel[key].mean():+.4f} a bet) | left to settle {sel['settle'].sum():+9.2f} | beat settling {beat:6d}, lost to it {lost:6d} | days positive {int((per_day > 0).sum())} of {ndays}")


print(f"window: {df['day'].min()} to {df['day'].max()}; {len(df)} filled resting buys on {df['ticker'].nunique()} markets. One contract a bet; the buy and the 20-cent sale carry no fee; the expired sale at the bid carries the taker fee. Average spread {df['spread'].mean():.3f}.")
show("no expiration (left to settle)", df, "settle")
print("--- expired bets sold at the bid, fee in; the expiration as a share of the time left at the fill")
for share, name in SHARES:
    show(f"after {name}", df, f"take_{share}")
print("--- expired bets sold by a resting sale at the ask, no fee (best case)")
for share, name in SHARES:
    show(f"after {name}", df, f"rest_{share}")
for share, name in SHARES:
    key = f"take_{share}"
    print(f"=== sold at the bid after {name} of the time left")
    print("  --- by the buy price")
    for lo, hi in ((0.0, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.30)):
        show(f"buy {lo:.2f} to {hi:.2f}", df[(df["buy"] > lo) & (df["buy"] <= hi)], key)
    print("  --- by the time left when the bid goes in")
    for lo, hi in ((180, 240), (120, 180), (60, 120), (0, 60)):
        show(f"{lo} to {hi} s left", df[(df["left"] > lo) & (df["left"] <= hi)], key)
    print("  --- by the changes of favourite in the five minutes before")
    for lo, hi, nm in ((1, 2, "1 or 2"), (3, 4, "3 or 4"), (5, 10 ** 6, "5 or more")):
        show(f"changes {nm}", df[(df["changes"] >= lo) & (df["changes"] <= hi)], key)
print("DONE")
