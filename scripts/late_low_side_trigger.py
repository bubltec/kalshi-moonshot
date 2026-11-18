"""Low sides in the last four minutes: is there a moment to buy that the curve shows beforehand? (2026-11-18, user: "Yes, find it", after scripts/late_low_side_increments.py found the 20-cent rises but only from a low known afterwards)

One question on the same moments as scripts/late_low_side_bursts.py: every 10 seconds of the last four minutes, a side whose ask is at or under 0.30, in a market whose favourite changed in the five minutes before. Bought at the ask, sale 20 cents higher, no fee on either leg (the kindest fill: a resting bid does worse); an unsold bet is counted at its settlement.
Instead of reading one split at a time, one additive fit over all the curve readings at once, made on the first five days (2026-11-09 to 11-13) and scored on the last three (11-14 to 11-16), which the fit never saw. If any combination of readings marks good moments, the moments the fit ranks highest on the unseen days are positive.
The fit: ridge regression of the bet's net on each reading cut into fifths (cuts from the fitting days only). numpy only.
Readings, in the market's typical minutes (rms one-second move of the mid so far, times sqrt(60)) unless said:
  buy (price), left (s), changes (of favourite, 5 min), since (s since the last change), unit (typical minute)
  slope5, slope15, slope30: the side's move over the last 5, 15, 30 s
  lift / drop: over its low / under its high of the last 60 s;  swing: that high less low
  fall120: how far the side stands under its high of the last 120 s, in cents (was it a favourite a moment ago?)
  hot: rms one-second move over the last 30 s over the market's own so far
  spread (cents); book: the side's bid size over bid plus ask size
  rises: 20-cent rises (any ask to a later bid, either side) finished in this market in the five minutes before
Run on the Mac: .venv/bin/python scripts/late_low_side_trigger.py > studies/results/<date>-late-low-side-trigger.txt"""
import csv
import glob
import math

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

BUY, GAIN, LAST, BACK, STEP = 0.30, 0.20, 240, 300.0, 10
TEST_DAYS = ("2026-11-14", "2026-11-15", "2026-11-16")
res = {r[0]: (float(r[1]), r[2]) for r in csv.reader(open("data/tape_archive/windows.csv")) if len(r) >= 3 and r[2] in ("yes", "no")}


def rise_times(ts, b, a):
    out, low = [], None
    for j in range(len(ts)):
        if low is not None and b[j] >= low + GAIN - 1e-9:
            out.append(ts[j])
            low = None
        elif a[j] > 0.01 and (low is None or a[j] < low):
            low = a[j]
    return out


rows = []
days = sorted(glob.glob("data/tape_archive/quotes/day=*/part-0.parquet"))
for f in days:
    day = f.split("day=")[1][:10]
    t = pq.read_table(f, columns=["ts", "ticker", "bid", "ask", "bid_size", "ask_size"])
    t = t.filter(pc.match_substring(t["ticker"], "15M-"))
    df = t.to_pandas().sort_values(["ticker", "ts"])
    for ticker, g in df.groupby("ticker", sort=False):
        if ticker not in res or len(g) < 60:
            continue
        close, result = res[ticker]
        ts = g["ts"].to_numpy()
        yb, ya = g["bid"].to_numpy(), g["ask"].to_numpy()
        bs, az = g["bid_size"].fillna(0).to_numpy(), g["ask_size"].fillna(0).to_numpy()
        ok = (yb > 0) & (ya > 0) & (ya <= 1) & (ya >= yb) & (ts < close)
        ts, yb, ya, bs, az = ts[ok], yb[ok], ya[ok], bs[ok], az[ok]
        if len(ts) < 60 or ts[0] > close - 600:
            continue
        mid = (yb + ya) / 2.0
        fav = pd.Series(np.where(yb >= 0.50, 1.0, np.where(ya <= 0.50, -1.0, np.nan))).ffill().to_numpy()
        change = np.zeros(len(ts), dtype=bool)
        change[1:] = (fav[1:] != fav[:-1]) & ~np.isnan(fav[1:]) & ~np.isnan(fav[:-1])
        cts = ts[change]
        if not len(cts) or cts[-1] < close - LAST - BACK:
            continue
        rts = np.array(sorted(rise_times(ts, yb, ya) + rise_times(ts, 1.0 - ya, 1.0 - yb)))
        secs = np.arange(close - 840, close)
        at = np.searchsorted(ts, secs, side="right") - 1
        have = at >= 0
        msec = np.where(have, mid[np.maximum(at, 0)], np.nan)
        d2 = np.where(have[1:] & have[:-1], np.diff(msec) ** 2, np.nan)
        for left in range(LAST, 0, -STEP):
            now = close - left
            i = int(np.searchsorted(ts, now, side="right") - 1)
            k = 840 - left
            if i < 20 or now - ts[i] > 30:
                continue
            n_cross = int(np.sum((cts > now - BACK) & (cts <= now)))
            if n_cross < 1:
                continue
            seen = d2[:k][~np.isnan(d2[:k])]
            if len(seen) < 120 or np.isnan(msec[k - 120:k + 1]).any():
                continue
            unit = float(np.sqrt(np.mean(seen)) * math.sqrt(60.0))
            if unit <= 0:
                continue
            hot = float(np.sqrt(np.mean(d2[k - 30:k])) * math.sqrt(60.0)) / unit
            since = now - float(cts[cts <= now][-1])
            n_rises = int(np.sum((rts > now - BACK) & (rts <= now))) if len(rts) else 0
            for side, b, a, sgn in (("yes", yb, ya, 1.0), ("no", 1.0 - ya, 1.0 - yb, -1.0)):
                buy = float(a[i])
                if not (0.02 < buy <= BUY):
                    continue
                m = msec if sgn > 0 else 1.0 - msec
                w60, w120 = m[k - 60:k + 1], m[k - 120:k + 1]
                size_b, size_a = (bs[i], az[i]) if sgn > 0 else (az[i], bs[i])
                hit = bool(np.any(b[i + 1:] >= buy + GAIN - 1e-9))
                rows.append({"day": day, "ticker": ticker, "buy": buy, "left": float(left), "changes": float(n_cross), "since": since, "unit": unit,
                             "slope5": (m[k] - m[k - 5]) / unit, "slope15": (m[k] - m[k - 15]) / unit, "slope30": (m[k] - m[k - 30]) / unit,
                             "lift": (m[k] - w60.min()) / unit, "drop": (w60.max() - m[k]) / unit, "swing": (w60.max() - w60.min()) / unit,
                             "fall120": float(w120.max() - m[k]), "hot": hot, "spread": buy - float(b[i]), "book": float(size_b / (size_b + size_a)) if size_b + size_a > 0 else 0.5,
                             "rises": float(n_rises), "net": GAIN if hit else float(result == side) - buy, "hit": hit})

df = pd.DataFrame(rows)
FEATS = ["buy", "left", "changes", "since", "unit", "slope5", "slope15", "slope30", "lift", "drop", "swing", "fall120", "hot", "spread", "book", "rises"]
test = df["day"].isin(TEST_DAYS)
tr, te = df[~test], df[test]
cuts = {c: np.unique(tr[c].quantile([0.2, 0.4, 0.6, 0.8]).to_numpy()) for c in FEATS}


def design(d):
    cols, names = [np.ones(len(d))], ["base"]
    for c in FEATS:
        which = np.searchsorted(cuts[c], d[c].to_numpy(), side="left")
        for j in range(1, len(cuts[c]) + 1):  # the lowest fifth is the reference
            cols.append((which == j).astype(float))
            names.append(f"{c} " + (f"over {cuts[c][j - 1]:.3g}" if j == len(cuts[c]) else f"{cuts[c][j - 1]:.3g} to {cuts[c][j]:.3g}"))
    return np.column_stack(cols), names


X, names = design(tr)
lam = 50.0 * np.eye(X.shape[1])
lam[0, 0] = 0.0
beta = np.linalg.solve(X.T @ X + lam, X.T @ tr["net"].to_numpy())
te = te.assign(score=design(te)[0] @ beta)
tr = tr.assign(score=X @ beta)


def line(label, sel):
    if not len(sel):
        print(f"  {label:26s} moments      0")
        return
    need = (sel["buy"] / (sel["buy"] + GAIN)).mean()
    per_day = sel.groupby("day")["net"].sum()
    print(f"  {label:26s} moments {len(sel):6d} on {sel['ticker'].nunique():5d} markets | avg buy {sel['buy'].mean():.3f} | rise {100.0 * sel['hit'].mean():5.1f}% (needs about {100.0 * need:.0f}%) | net {sel['net'].sum():+8.2f} ({sel['net'].mean():+.3f} each) | days positive {int((per_day > 0).sum())} of {len(per_day)}")


print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {len(df)} moments on {df['ticker'].nunique()} markets; fitted on {len(tr)} moments ({tr['day'].min()} to {tr['day'].max()}), scored on {len(te)} unseen ({te['day'].min()} to {te['day'].max()}). One contract a moment, bought at the ask, sale {GAIN:.2f} higher, no fee.")
for name, d in (("the fitting days (seen: flattering)", tr), ("the unseen days", te)):
    print(f"--- {name}, by the fit's ranking of the moments")
    line("all", d)
    order = d.sort_values("score", ascending=False)
    for share in (0.5, 0.2, 0.1, 0.05, 0.02):
        line(f"top {100 * share:g}% by the fit", order.head(int(len(order) * share)))
    line("bottom 20% by the fit", order.tail(int(len(order) * 0.2)))
print("--- what the fit leans on: the largest effects on the net per contract, against each reading's lowest fifth")
for j in sorted(range(1, len(beta)), key=lambda j: -abs(beta[j]))[:16]:
    print(f"  {names[j]:34s} {beta[j]:+.3f}")
print("DONE")
