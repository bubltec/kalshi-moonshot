"""Commodity favourites bought fee-free with a resting bid: how often does it fill, and what do the bets that fill pay? (2026-11-18, user: "Yes, study it", after scripts/commodity_favourite_round_trips.py found the buy's taker fee is $62 of the $85 held loss and holding with no fee is about break-even)

One question on the same bets as scripts/commodity_favourite_round_trips.py (a commodity side first quoted from 0.60 to 0.92 at a 30 s mark with 3 to 13 min left, one a market and side, held to settlement): instead of buying at the ask with the taker fee, a bid rests at the side's BID (post-only, a maker, no fee) for a share of the time left. If it fills the bet is held to settlement. Two ways to end the wait:
  rest only     an unfilled bid is no bet
  fall back     an unfilled bid is cancelled at the end of the window and the side is bought at its ask THEN with the taker fee (what a lane with a fallback buy does)
The window is a share of the time left at the mark, never fixed seconds: an eighth or a quarter (stated before the run).
Fills come from the quotes (the side's ask came down to our bid) and from Kalshi's printed trades (tape flow_1s: a taker traded against our side at our price or better: first in the queue, the best case; strict: only THROUGH our price: last in the queue, the worst). The truth lies between them.
Reported against the same bets bought at once with the fee: bets, filled share, the filled bets' win rate and net per $100 staked, what the UNFILLED ones would have paid if bought at once (the bets the wait loses: if they are the winners, the fill is adverse), the fall-back total, days positive of 8. The splits (ask band, time left, changes of favourite so far) are chosen after seeing the totals and said so.
Tape: data/tape_archive/quotes and flow_1s, 2026-11-09 to 11-16, windows.csv. Kalshi's own data only. Nothing here is keyed on a market name.
Run on the Mac: .venv/bin/python scripts/commodity_favourite_resting_buy.py > studies/results/<date>-commodity-favourite-resting-buy.txt"""
import csv
import glob
import math

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

FEE = 0.07
E = 1e-9
LO, HI = 0.60, 0.92
SERIES = ("KXBRENT15M", "KXWTI15M", "KXNATGAS15M", "KXGOLD15M", "KXSILVER15M", "KXCOPPER15M", "KXPLATINUM15M", "KXPALLADIUM15M")
SHARES = ((0.125, "an eighth"), (0.25, "a quarter"))


def fee(p):
    return FEE * p * (1.0 - p)


def first_after(times, mask, after, before):
    idx = np.flatnonzero(mask & (times > after) & (times < before))
    return float(times[idx[0]]) if len(idx) else math.inf


res = {r[0]: (float(r[1]), r[2]) for r in csv.reader(open("data/tape_archive/windows.csv")) if len(r) >= 3 and r[2] in ("yes", "no")}
rows = []
days = sorted(glob.glob("data/tape_archive/quotes/day=*/part-0.parquet"))
for f in days:
    day = f.split("day=")[1][:10]
    fl = pq.read_table(f.replace("/quotes/", "/flow_1s/"), columns=["ts", "ticker", "yes_vol", "no_vol", "vwap"])
    fl = fl.filter(pc.match_substring(fl["ticker"], "15M-")).to_pandas().dropna(subset=["vwap"]).sort_values(["ticker", "ts"])
    flow = {tk: (g["ts"].to_numpy(dtype=float), g["yes_vol"].to_numpy(), g["no_vol"].to_numpy(), g["vwap"].to_numpy()) for tk, g in fl.groupby("ticker", sort=False)}
    t = pq.read_table(f, columns=["ts", "ticker", "bid", "ask"])
    t = t.filter(pc.match_substring(t["ticker"], "15M-"))
    df = t.to_pandas()
    df = df[df["ticker"].str.split("-").str[0].isin(SERIES)].sort_values(["ticker", "ts"])
    for ticker, g in df.groupby("ticker", sort=False):
        if ticker not in res or ticker not in flow or len(g) < 120:
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
        fts, yv, nv, vw = flow[ticker]
        secs = np.arange(close - 900, close)
        at = np.searchsorted(ts, secs, side="right") - 1
        have = at >= 0
        msec = np.where(have, mid[np.maximum(at, 0)], np.nan)
        d2 = np.where(have[1:] & have[:-1], np.diff(msec) ** 2, np.nan)
        taken = set()
        for left in range(780, 170, -30):
            now = close - left
            k = 900 - left
            i = int(np.searchsorted(ts, now, side="right") - 1)
            if i < 20 or now - ts[i] > 30 or k < 300:
                continue
            seen = d2[:k][~np.isnan(d2[:k])]
            if len(seen) < 120 or math.sqrt(float(np.mean(seen))) <= 0:
                continue
            flips = int(np.sum(cts <= now))
            for side, b, a, sp, into_bid in (("yes", yb, ya, vw, nv), ("no", 1.0 - ya, 1.0 - yb, 1.0 - vw, yv)):
                ask, p = float(a[i]), float(b[i])
                if side in taken or not (LO <= ask <= HI) or p < 0.5:
                    continue
                taken.add(side)
                won = result == side
                now_net = (1.0 if won else 0.0) - ask - fee(ask)
                row = {"day": day, "ticker": ticker, "ask": ask, "bid": p, "left": float(left), "flips": float(flips), "won": won, "now": now_net}
                for share, _ in SHARES:
                    until = now + share * left
                    for kind in ("kind", "strict"):
                        through = first_after(ts, a <= p + E, now, until)
                        prints = first_after(fts, (into_bid > 0) & ((sp <= p + E) if kind == "kind" else (sp < p - E)), math.floor(now), until)
                        tf = min(through, prints)
                        key = f"{share}_{kind}"
                        row[f"{key}_filled"] = tf < math.inf
                        row[f"{key}_net"] = ((1.0 if won else 0.0) - p) if tf < math.inf else np.nan  # a maker: no fee
                        j = int(np.searchsorted(ts, until, side="right") - 1)
                        late = float(a[j])
                        row[f"{key}_fall"] = row[f"{key}_net"] if tf < math.inf else ((1.0 if won else 0.0) - late - fee(late))
                        row[f"{key}_fall_stake"] = p if tf < math.inf else late
                rows.append(row)

df = pd.DataFrame(rows)
ndays = df["day"].nunique()


def line(label, sel, key):
    if len(sel) < 40:
        print(f"  {label:30s} bets {len(sel):5d}")
        return
    got = sel[sel[f"{key}_filled"]]
    miss = sel[~sel[f"{key}_filled"]]
    stake = got["bid"].sum()
    per_day = got.groupby("day")[f"{key}_net"].sum()
    fall = sel[f"{key}_fall"].sum()
    fall_stake = sel[f"{key}_fall_stake"].sum()
    print(f"  {label:30s} bets {len(sel):5d} | filled {len(got):5d} ({100.0 * len(got) / len(sel):4.1f}%) win {100.0 * got['won'].mean() if len(got) else 0:5.1f}% | filled net {got[f'{key}_net'].sum():+8.2f} ({100.0 * got[f'{key}_net'].sum() / stake if stake else 0:+5.1f} per $100, days positive {int((per_day > 0).sum())} of {ndays}) | unfilled {len(miss):5d} would have paid {miss['now'].sum():+7.2f} at once (win {100.0 * miss['won'].mean() if len(miss) else 0:4.1f}%) | with the fall-back {fall:+8.2f} ({100.0 * fall / fall_stake:+5.1f} per $100) | all at once {sel['now'].sum():+8.2f} ({100.0 * sel['now'].sum() / sel['ask'].sum():+5.1f} per $100)")


print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {len(df)} commodity favourites on {df['ticker'].nunique()} markets (first quote at {LO:.2f} to {HI:.2f}, 3 to 13 min left, a bid of 0.50 or more), one contract each, held to settlement. A resting bid sits at the side's bid, no fee; at once = bought at the ask with the taker fee. Average spread {(df['ask'] - df['bid']).mean():.3f}.")
for share, name in SHARES:
    for kind, kname in (("kind", "best case: a print at our price fills us"), ("strict", "worst case: only a print through our price")):
        print(f"=== the bid rests {name} of the time left; {kname}")
        line("all", df, f"{share}_{kind}")
for share, name in SHARES[:1]:
    key = f"{share}_kind"
    print(f"=== the bid rests {name} of the time left, best case, split (chosen after seeing the totals)")
    print("  --- by the ask")
    for lo, hi in ((0.60, 0.70), (0.70, 0.80), (0.80, 0.86), (0.86, 0.92001)):
        line(f"ask {lo:.2f} to {hi:.2f}", df[(df["ask"] >= lo) & (df["ask"] < hi)], key)
    print("  --- by the time left at the buy")
    for lo, hi in ((170, 300), (300, 450), (450, 600), (600, 781)):
        line(f"{lo} to {hi} s left", df[(df["left"] >= lo) & (df["left"] < hi)], key)
    print("  --- by flips (changes of favourite so far)")
    for lo, hi, nm in ((0, 1, "0 or 1"), (2, 6, "2 to 6"), (7, 10 ** 6, "7 or more")):
        line(f"flips {nm}", df[(df["flips"] >= lo) & (df["flips"] <= hi)], key)
print("DONE")
