"""The user's early-window rise trade, repeated on the tape. (2026-11-19, user: "I just did it ... look at this behavior and repeat it"; "i think i missed the fee free buy but it could have been easy")

The trade: KXBTC15M-26NOV190800-00, YES bought 5 at 0.65 at 07:46:27 (87 s after the window opened at 07:45; the quote went 0.60 at 07:45:30, 0.57 at 07:46:00, 0.65, 0.72, 0.85, 0.89 by 07:48:00) and sold by a limit order. Read as: early in the window, the side has turned up off its low and is in the 0.60 to 0.80 band; buy it, and sell into the rise a typical minute or two above.
One question on the archived Kalshi quote curves (all crypto series, 2026-11-09 to the last archived day): at each 15 s mark from 60 to 420 s into the window, a SETUP is: the side's mid (YES or NO, whichever is in the band) rose at least 0.5 typical minutes over the last 60 s, the side's ask is 0.60 to 0.80, and it is at least 0.5 typical minutes above its low since the open. Typical minute = rms one-second mid move times sqrt(60) from the seconds BEFORE the mark (at least 45 of them). The first setup per window, coin and way counts. Two entries: TAKER (buy at the ask now, pay the fee 0.07 p (1 - p)) and MAKER (a resting bid at the bid, no fee, waits 10% of the time left, fills only if the ask comes down to it: the 'missed fee-free buy' is the share that never fills). The sell rests at the fill plus TARGET (0.5, 1.0, 1.5 typical minutes, at least 1c; no fee), for 25% of the time left, never past the last 60 s; unsold is cut at the bid with the taker fee or held to the result. Reported: setups, the maker fill rate, sells filled, net per setup in cents and per $100 staked for each entry and target (cut and hold), the same split by seconds into the window (60-180, 180-300, 300-420), by day, and the 'no stop' baseline of the same scalps started at any moment in the same early seconds. Counts overlap in time. Settings chosen after seeing the results are said to be so.
Run on the Mac: .venv/bin/python scripts/crypto_early_rise_scalps.py > studies/results/<date>-crypto-early-rise-scalps.txt"""
import collections
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
chains = []
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
        for ticker, a in tk.items():
            d2 = np.where(np.isnan(a["d2"]), 0.0, a["d2"])
            ok2 = (~np.isnan(a["d2"])).astype(float)
            cs, cn = np.cumsum(d2), np.cumsum(ok2)
            ask, bid = a["a"], a["b"]
            for way in ("yes", "no"):
                sa = ask if way == "yes" else 1.0 - bid
                sb = bid if way == "yes" else 1.0 - ask
                sm = (sa + sb) / 2.0
                win = a["result"] == way
                seen_setup = False
                for k in range(60, 421, 15):
                    if seen_setup:
                        break
                    if cn[k - 1] < 45 or cs[k - 1] <= 0 or np.isnan(sm[k - 60:k + 1]).any() or np.isnan(sa[k]) or not (0.60 <= sa[k] <= 0.80):
                        continue
                    unit = float(np.sqrt(cs[k - 1] / cn[k - 1]) * math.sqrt(60.0))
                    rise60 = float(sm[k] - sm[k - 60]) / unit
                    low = float(np.nanmin(sm[10:k + 1]))
                    if rise60 < 0.5 or (float(sm[k]) - low) / unit < 0.5:
                        continue
                    seen_setup = True
                    left = 900 - k
                    wait, hold = int(0.10 * left), int(0.25 * left)
                    out = {}
                    for entry in ("taker", "maker"):
                        if entry == "taker":
                            jf, pf, fee_in = k, float(sa[k]), 0.07 * float(sa[k]) * (1 - float(sa[k]))
                        else:
                            pb = float(sb[k])
                            h = np.flatnonzero(sa[k + 1:k + 1 + wait] <= pb + 1e-9)
                            if not len(h):
                                out[entry] = None
                                continue
                            jf, pf, fee_in = k + 1 + int(h[0]), pb, 0.0
                        end = min(jf + hold, 840)
                        seg = sb[jf + 1:end + 1]
                        for tu in (0.5, 1.0, 1.5):
                            tgt = max(0.01, tu * unit)
                            hh = np.flatnonzero(seg >= pf + tgt - 1e-9)
                            if len(hh):
                                out[(entry, tu)] = ("sold", tgt - fee_in, tgt - fee_in, pf)
                            else:
                                cb = float(sb[end]) if not np.isnan(sb[end]) else pf
                                out[(entry, tu)] = ("open", cb - pf - 0.07 * cb * (1 - cb) - fee_in, (1.0 if win else 0.0) - pf - fee_in, pf)
                        out[entry] = True
                    feats = {"rise60": rise60, "above low": (float(sm[k]) - low) / unit, "last 15 s": float(sm[k] - sm[k - 15]) / unit, "spread c": 100.0 * float(sa[k] - sb[k]), "price": float(sa[k]), "typical minute": unit, "seconds in": float(k), "last 15 s share of 60 s": float(sm[k] - sm[k - 15]) / max(float(sm[k] - sm[k - 60]), 1e-9)}
                    rows.append((day, k, unit, float(sa[k]), out, feats))
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {len(rows)} early-rise setups (first per window, coin and way), seconds 60-420 into the window, band 0.60-0.80, rise 0.5+ unit in 60 s and 0.5+ unit above the open's low")
mk = [r for r in rows if r[4].get("maker")]
print(f"maker bid filled: {len(mk)} of {len(rows)} ({100.0 * len(mk) / len(rows):.1f}%)  (the rest is the missed fee-free buy: the ask never came down to the bid)")


def report(sel, entry, tu, name):
    sel = [r for r in sel if (entry, tu) in r[4]]
    if not sel:
        return
    sold = sum(1 for r in sel if r[4][(entry, tu)][0] == "sold")
    nc = sum(r[4][(entry, tu)][1] for r in sel)
    nh = sum(r[4][(entry, tu)][2] for r in sel)
    stake = sum(r[4][(entry, tu)][3] for r in sel)
    print(f"  {name:30s} n {len(sel):5d} | sold {100.0 * sold / len(sel):4.1f}% | net per setup cut {100.0 * nc / len(sel):+6.2f}c hold {100.0 * nh / len(sel):+6.2f}c | per $100 staked cut {100.0 * nc / stake:+6.1f}")


for entry in ("taker", "maker"):
    print(f"--- {entry} entry")
    for tu in (0.5, 1.0, 1.5):
        report(rows, entry, tu, f"target {tu} typical min, all")
for entry in ("taker", "maker"):
    print(f"--- {entry} entry, target 1.0, by seconds into the window")
    for lo, hi in ((60, 180), (180, 300), (300, 421)):
        report([r for r in rows if lo <= r[1] < hi], entry, 1.0, f"{lo}-{hi} s")
    print(f"--- {entry} entry, target 1.0, by day")
    for d in sorted(set(r[0] for r in rows)):
        report([r for r in rows if r[0] == d], entry, 1.0, d)

print("--- what separates a setup that sells from one that does not (taker entry, target 1.0): thirds of each reading known at the buy; sold share and net per setup in cents (cut). A look for a curve reading, not a rule")
for name in rows[0][5]:
    vals = np.array([r[5][name] for r in rows])
    cuts = np.quantile(vals, [1 / 3, 2 / 3])
    parts = []
    for lo, hi in ((-1e9, cuts[0]), (cuts[0], cuts[1]), (cuts[1], 1e9)):
        sel = [r for r in rows if lo <= r[5][name] < hi]
        sold = sum(1 for r in sel if r[4][("taker", 1.0)][0] == "sold") / max(len(sel), 1)
        nc = sum(r[4][("taker", 1.0)][1] for r in sel) / max(len(sel), 1)
        parts.append(f"{sold * 100:4.1f}% {100.0 * nc:+5.2f}c")
    print(f"  {name:24s} cuts {cuts[0]:7.3f} {cuts[1]:7.3f} | low third {parts[0]} | middle {parts[1]} | high {parts[2]}")
