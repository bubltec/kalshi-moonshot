"""Late favourite buys against the coin trend. (2026-11-19, user: "the coin trend. they usually move together, not always. and late market buys are risk on a coin trend because they don't all sync at the same time and have blips")

One question on the archived Kalshi quote curves (all crypto series, 2026-11-09 to the last archived day, whole market length, 60 s marks): a favourite side (ask 0.60 to 0.85, spread 4c or less) bought at the ask and held to settlement (fee 0.07 p (1 - p) on the buy), split by whether the GROUP of the other coins is moving with it. Group move = the mean over the other coins (at least 4 quoting) of the last 3 minutes' YES-mid change in each coin's own typical minutes (rms one-second move from before the mark, times sqrt(60)), signed toward the side bought: WITH (group moved 0.5 typical minutes or more toward the side), FLAT (within 0.5), AGAINST (0.5 or more away from the side). Also whether the coin's own last 3 minutes agreed with the group (own move and group move the same sign: IN SYNC) or not (OUT OF SYNC: a blip or a lag). Reported by seconds left (13-10, 10-7, 7-4, 4-2, 2-0.5 minutes): bets, mean ask, win rate, win rate less ask, net per $100; and by day for the late buckets. Counts are marks (overlapping in time, coins of one wave overlap); the group is made of the same wave. No coin is named. Settings chosen after seeing the results are said to be so.
Run on the Mac: .venv/bin/python scripts/crypto_late_buy_sync.py > studies/results/<date>-crypto-late-buy-sync.txt"""
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
rows = []
rows = []
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
        coins = {}
        for ticker, a in tk.items():
            d2 = np.where(np.isnan(a["d2"]), 0.0, a["d2"])
            coins[ticker] = (a, np.cumsum(d2), np.cumsum((~np.isnan(a["d2"])).astype(float)))
        if len(coins) < 5:
            continue
        for k in range(240, 870, 60):
            mv = {}
            for ticker, (a, cs, cn) in coins.items():
                m = a["m"]
                if cn[k - 1] < 120 or cs[k - 1] <= 0 or np.isnan(m[k]) or np.isnan(m[k - 180]):
                    continue
                u = float(np.sqrt(cs[k - 1] / cn[k - 1]) * math.sqrt(60.0))
                mv[ticker] = (m[k] - m[k - 180]) / u
            if len(mv) < 5:
                continue
            for ticker, own in mv.items():
                a = coins[ticker][0]
                others = [v for t2, v in mv.items() if t2 != ticker]
                if len(others) < 4:
                    continue
                grp = float(np.mean(others))
                for way in ("yes", "no"):
                    sa = a["a"][k] if way == "yes" else 1.0 - a["b"][k]
                    sb = a["b"][k] if way == "yes" else 1.0 - a["a"][k]
                    if np.isnan(sa) or np.isnan(sb) or not (0.60 <= sa < 0.85) or sa - sb > 0.04:
                        continue
                    sg = 1.0 if way == "yes" else -1.0
                    rows.append((day, 900 - k, float(sa), a["result"] == way, sg * grp, (own * grp) > 0))
n = len(rows)
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {n} favourite-side marks (ask 0.60-0.85, spread <= 4c) with 4+ other coins quoting, held to settlement")
R = np.array([(r[1], r[2], 1.0 if r[3] else 0.0, r[4], 1.0 if r[5] else 0.0) for r in rows])
left, ask, won, grp, sync = R.T
net = (won - ask - 0.07 * ask * (1 - ask)) / ask
buckets = ((780, 601, "13 to 10 min"), (600, 421, "10 to 7 min"), (420, 241, "7 to 4 min"), (240, 121, "4 to 2 min"), (120, 30, "2 to 0.5 min"))
states = (("group WITH the side", grp >= 0.5), ("group FLAT", np.abs(grp) < 0.5), ("group AGAINST the side", grp <= -0.5))
print("--- by seconds left and the group's last 3 minutes against the side: bets | mean ask | win rate | win less ask | net per $100")
for hi, lo, nm in buckets:
    for sn, sm in states:
        m = (left <= hi) & (left >= lo) & sm
        if m.sum() >= 80:
            print(f"  {nm:13s} {sn:22s} | {int(m.sum()):6d} | {ask[m].mean():.3f} | {100 * won[m].mean():5.1f}% | {100 * (won[m].mean() - ask[m].mean()):+5.1f}c | {100 * net[m].mean():+6.1f}")
print("--- in sync or not (the coin's own last 3 minutes and the group's the same direction), by seconds left")
for hi, lo, nm in buckets:
    for sn, sm in (("in sync", sync == 1), ("out of sync", sync == 0)):
        m = (left <= hi) & (left >= lo) & sm
        if m.sum() >= 80:
            print(f"  {nm:13s} {sn:12s} | {int(m.sum()):6d} | win {100 * won[m].mean():5.1f}% less ask {100 * (won[m].mean() - ask[m].mean()):+5.1f}c | net per $100 {100 * net[m].mean():+6.1f}")
print("--- by day, seconds left 7 to 0.5 minutes: net per $100 with the group / flat / against")
dd = np.array([r[0] for r in rows])
late = left <= 420
for d in sorted(set(dd)):
    parts = []
    for sn, sm in states:
        m = late & sm & (dd == d)
        parts.append(f"{100 * net[m].mean():+6.1f} ({int(m.sum())})" if m.sum() >= 30 else "   n/a")
    print(f"  {d} {' | '.join(parts)}")
