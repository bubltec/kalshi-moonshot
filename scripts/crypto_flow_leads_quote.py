"""Does the coins' own trade flow lead the quote? (2026-11-19, user: "liquidation prints: a cascade shows up in the coins' own order flow before the quote moves ... good points to look at")

One question on Kalshi's own trade prints (data/tape_archive/flow_1s: per second and ticker the YES-side and NO-side taker volume) and quotes (all crypto series, 2026-11-09 to the last archived day): at every 5 s mark from 2 to 14.5 minutes into a window, with at least 6 coins quoting, each coin's NET FLOW over the last 15 s = YES volume less NO volume, in units of that coin's own flow scale (the mean absolute 15 s net flow over the window's earlier seconds, at least 60 of them: a threshold from the market's own recent history, never a fixed size); JOINT FLOW = the mean of those over the coins. Joint PAST = the mean over coins of the last 15 s mid move, and joint FUTURE 15 s and 30 s = the mean of the following mid moves, all in each coin's typical minutes (rms one-second move times sqrt(60), from before the mark). 
A. By joint flow (fifths of its own distribution, with the extremes: the top and bottom 5%): the mean past and future mid moves, signed so that positive = up. If flow leads, the future move follows the flow's sign.
B. The same only where the quote has NOT yet moved (|joint past| under 0.3 typical minutes): flow before the quote.
C. By day for the top and bottom 5% of joint flow in B (does the sign hold every day).
Marks overlap in time and coins move together, so counts are marks, not independent events; a result needs the same sign every day. Settings chosen after seeing the results are said to be so.
Run on the Mac: .venv/bin/python scripts/crypto_flow_leads_quote.py > studies/results/<date>-crypto-flow-leads-quote.txt"""
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
    fl = pq.read_table(f.replace("quotes", "flow_1s"), columns=["ts", "ticker", "yes_vol", "no_vol"])
    fl = fl.filter(pc.match_substring(fl["ticker"], "15M-")).to_pandas()
    flows = {k: g for k, g in fl.groupby("ticker")}
    for close, tk in wins.items():
        if len(tk) < 6:
            continue
        coin = {}
        for ticker, a in tk.items():
            g = flows.get(ticker)
            net = np.zeros(900)
            if g is not None:
                sec = (g["ts"].to_numpy() - (close - 900)).astype(int)
                okk = (sec >= 0) & (sec < 900)
                np.add.at(net, sec[okk], (g["yes_vol"].to_numpy() - g["no_vol"].to_numpy())[okk])
            d2 = np.where(np.isnan(a["d2"]), 0.0, a["d2"])
            coin[ticker] = (a, net, np.cumsum(d2), np.cumsum(np.concatenate([[0.0], net]))[:900 + 1], np.cumsum((~np.isnan(a["d2"])).astype(float)))
        for k in range(120, 870, 5):
            fz, pa, f15, f30 = [], [], [], []
            for ticker, (a, net, cs, cnet, cn) in coin.items():
                m = a["m"]
                if np.isnan(m[[k - 15, k, k + 15 if k + 15 < 900 else k, k + 30 if k + 30 < 900 else k]]).any() or cn[k - 1] < 60 or cs[k - 1] <= 0:
                    continue
                u = float(np.sqrt(cs[k - 1] / cn[k - 1]) * math.sqrt(60.0))
                seen = np.array([cnet[j] - cnet[j - 15] for j in range(30, k, 5)])
                scale = float(np.mean(np.abs(seen))) if len(seen) >= 6 else 0.0
                if scale <= 0:
                    continue
                fz.append(float(cnet[k] - cnet[k - 15]) / scale)
                pa.append((m[k] - m[k - 15]) / u)
                f15.append((m[min(k + 15, 899)] - m[k]) / u)
                f30.append((m[min(k + 30, 899)] - m[k]) / u)
            if len(fz) >= 6 and k + 30 < 900:
                rows.append((day, float(np.mean(np.clip(fz, -10, 10))), float(np.mean(pa)), float(np.mean(f15)), float(np.mean(f30))))
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {len(rows)} marks (5 s apart, 2-14.5 min into a window, 6+ coins)")
R = np.array([[r[1], r[2], r[3], r[4]] for r in rows])
Fz, P, F15, F30 = R[:, 0], R[:, 1], R[:, 2], R[:, 3]
print(f"joint flow z: mean {Fz.mean():+.2f}, sd {Fz.std():.2f}; corr(flow, past move) {np.corrcoef(Fz, P)[0, 1]:+.3f}, corr(flow, next 15 s) {np.corrcoef(Fz, F15)[0, 1]:+.3f}, corr(flow, next 30 s) {np.corrcoef(Fz, F30)[0, 1]:+.3f}; corr(past move, next 15 s) {np.corrcoef(P, F15)[0, 1]:+.3f}")


def table(mask, title):
    z = Fz[mask]
    if len(z) < 100:
        print(f"--- {title}: only {len(z)} marks"); return
    q = np.quantile(z, [0.05, 0.2, 0.4, 0.6, 0.8, 0.95])
    print(f"--- {title} ({int(mask.sum())} marks)")
    for nm, lo, hi in (("bottom 5%", -1e9, q[0]), ("5-20%", q[0], q[1]), ("20-40%", q[1], q[2]), ("40-60%", q[2], q[3]), ("60-80%", q[3], q[4]), ("80-95%", q[4], q[5]), ("top 5%", q[5], 1e9)):
        s = mask & (Fz > lo) & (Fz <= hi)
        if s.sum() >= 20:
            print(f"  {nm:10s} n {int(s.sum()):5d} flow {Fz[s].mean():+6.2f} | past {P[s].mean():+.3f} | next 15 s {F15[s].mean():+.3f} | next 30 s {F30[s].mean():+.3f}")


table(np.ones(len(Fz), bool), "A. all marks")
table(np.abs(P) < 0.3, "B. the quote has not moved (|past 15 s| under 0.3 typical minutes)")
print("--- C. by day, in B: top 5% and bottom 5% of joint flow, the next 30 s joint move (typical minutes)")
mask = np.abs(P) < 0.3
q = np.quantile(Fz[mask], [0.05, 0.95])
days_ = np.array([r[0] for r in rows])
for d in sorted(set(days_)):
    mt = mask & (Fz >= q[1]) & (days_ == d)
    mb = mask & (Fz <= q[0]) & (days_ == d)
    if mt.sum() >= 10 and mb.sum() >= 10:
        print(f"  {d} top n {int(mt.sum()):4d} next 30 s {F30[mt].mean():+.3f} | bottom n {int(mb.sum()):4d} next 30 s {F30[mb].mean():+.3f}")
