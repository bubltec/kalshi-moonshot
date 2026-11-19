"""Is the all-coins-lose wave a shift from the window before? (2026-11-19, user: "it doesn't happen often, maybe once a day. but there is a shift like this and we can avoid these 'all coin losses' without restricting to 2 per side")

One question on the same favourite sets as crypto_wave_shapes.py (four or more coins on one way in the band 0.60 to 0.85, the first such 60 s mark in a window and way, 4 to 13 minutes left, 2026-11-09 to the last archived day): how many sets went "all lose" (75% or more of the set's coins lost, five or more coins for the SEVERE cut), how many a day, and does the way of the set agree with how the PREVIOUS window (15 minutes earlier, same series) settled? PREV = the share of the previous window's quoting coins that settled YES. A set on YES whose previous window was mostly yes (PREV high) is "with the last window"; on NO with PREV low likewise; the other cases are "against the last window" (the shift). Reported: sets by day and by all-lose, severe sets by day, and by with/against and by fifths of the previous window's agreement: sets, all-lose share, mean share lost, net per $100 of buying the whole set at the mark and holding (fee on every leg). Marks and windows are not independent; the counts are sets. Settings chosen after seeing the results are said to be so.
Run on the Mac: .venv/bin/python scripts/crypto_wave_prev_window.py > studies/results/<date>-crypto-wave-prev-window.txt"""
import csv
import glob
import math
from collections import defaultdict

import numpy as np
import pyarrow.compute as pc
import pyarrow.parquet as pq

LO, HI = 0.60, 0.85
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXZEC15M")
MARKS = list(range(780, 230, -60))
OFFS = list(range(-300, 301, 60))
res = {r[0]: (float(r[1]), r[2]) for r in csv.reader(open("data/tape_archive/windows.csv")) if len(r) >= 3 and r[2] in ("yes", "no")}
events = []
days = sorted(glob.glob("data/tape_archive/quotes/day=*/part-0.parquet"))
for f in days:
    day = f.split("day=")[1][:10]
    t = pq.read_table(f, columns=["ts", "ticker", "bid", "ask"])
    t = t.filter(pc.match_substring(t["ticker"], "15M-"))
    df = t.to_pandas()
    df = df[df["ticker"].str.split("-").str[0].isin(SERIES)].sort_values(["ticker", "ts"])
    wins = defaultdict(dict)  # close -> ticker -> arrays per second (mid, bid, ask, d2 prefix)
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
        asec = np.where(have, ya[np.maximum(at, 0)], np.nan)
        bsec = np.where(have, yb[np.maximum(at, 0)], np.nan)
        d = np.where(have[1:] & have[:-1], np.diff(msec), np.nan)
        wins[close][ticker] = {"m": msec, "a": asec, "b": bsec, "d2": d * d, "result": result}
    for close, tk in wins.items():
        if len(tk) < 6:
            continue
        for way in ("yes", "no"):
            for left in MARKS:
                k = 900 - left
                if k < 300:
                    continue
                coins = []
                for ticker, a in tk.items():
                    ask = a["a"][k] if way == "yes" else 1.0 - a["b"][k]
                    if np.isnan(ask) or not (LO <= ask <= HI):
                        continue
                    seen = a["d2"][:k][~np.isnan(a["d2"][:k])]
                    if len(seen) < 120:
                        continue
                    unit = float(np.sqrt(np.mean(seen)) * math.sqrt(60.0))
                    if unit <= 0:
                        continue
                    side = a["m"] if way == "yes" else 1.0 - a["m"]
                    if np.isnan(side[k - 300:k + 1]).any():
                        continue
                    coins.append((ticker, a, side, unit))
                if len(coins) < 4:
                    continue
                lost = [a["result"] != way for _, a, _, _ in coins]
                cost = pay = 0.0
                for (_, a, _, _), l in zip(coins, lost):
                    p = float(a["a"][k]) if way == "yes" else 1.0 - float(a["b"][k])
                    cost += p + 0.07 * p * (1 - p)
                    pay += 0.0 if l else 1.0
                events.append({"day": day, "close": close, "way": way, "n": len(coins), "lose": float(np.mean(lost)), "cost": cost, "pay": pay})
                break
n = len(events)
byclose = defaultdict(list)
for r_ticker, (c, rr) in res.items():
    byclose[c].append(rr)
for e in events:
    prev = byclose.get(e["close"] - 900.0) or byclose.get(e["close"] - 900)
    e["prev"] = (sum(1 for x in prev if x == "yes") / len(prev)) if prev and len(prev) >= 6 else None
    e["agree"] = None if e["prev"] is None else (e["prev"] if e["way"] == "yes" else 1.0 - e["prev"])  # share of the previous window's coins that settled on this set's way
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {n} favourite sets")
print("--- sets, all-lose sets and severe (5+ coins, 75%+ lost) sets by day")
for d in sorted(set(e["day"] for e in events)):
    sel = [e for e in events if e["day"] == d]
    print(f"  {d} sets {len(sel):3d} all lose {sum(1 for e in sel if e['lose'] >= 0.75):3d} severe {sum(1 for e in sel if e['lose'] >= 0.75 and e['n'] >= 5):3d}")


def show(name, sel):
    if len(sel) < 10:
        return
    cost, pay = sum(e["cost"] for e in sel), sum(e["pay"] for e in sel)
    print(f"  {name:34s} sets {len(sel):4d} | all lose {100.0 * np.mean([e['lose'] >= 0.75 for e in sel]):5.1f}% | severe {100.0 * np.mean([e['lose'] >= 0.75 and e['n'] >= 5 for e in sel]):5.1f}% | mean share lost {100.0 * np.mean([e['lose'] for e in sel]):5.1f}% | net per $100 {100.0 * (pay - cost) / cost:+6.1f}")


have = [e for e in events if e["agree"] is not None]
print(f"--- with or against the previous window ({len(have)} sets with a previous window of 6+ coins)")
show("all sets", have)
show("with the last window (agree>=0.6)", [e for e in have if e["agree"] >= 0.6])
show("middle (0.4 to 0.6)", [e for e in have if 0.4 < e["agree"] < 0.6])
show("against the last window (<=0.4)", [e for e in have if e["agree"] <= 0.4])
print("--- by fifths of the previous window's share on this way")
cuts = np.quantile([e["agree"] for e in have], [0.2, 0.4, 0.6, 0.8])
edges = [-1, *cuts.tolist(), 2]
for lo, hi in zip(edges[:-1], edges[1:]):
    show(f"prev agree {max(lo, 0):.2f} to {min(hi, 1):.2f}", [e for e in have if lo < e["agree"] <= hi])
print("--- by day, against the last window (<=0.4) vs the rest: all-lose share")
for d in sorted(set(e["day"] for e in have)):
    a_ = [e for e in have if e["day"] == d and e["agree"] <= 0.4]
    b_ = [e for e in have if e["day"] == d and e["agree"] > 0.4]
    print(f"  {d} against {len(a_):3d} all lose {100.0 * np.mean([e['lose'] >= 0.75 for e in a_]) if a_ else 0:5.1f}% | rest {len(b_):3d} all lose {100.0 * np.mean([e['lose'] >= 0.75 for e in b_]) if b_ else 0:5.1f}%")
