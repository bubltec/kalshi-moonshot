"""Fee-free scalps over and over in the 60-85c band. (2026-11-19, user: "shorter periods of fee free buys, multiple times per market ... in the 60-85c range and just buying and selling for small wins over and over")

One question on the archived Kalshi quote curves (all crypto series present, 2026-11-09 to the last archived day, whole market length): a chain of scalps per window and coin on the favourite side, side ask 0.60 to 0.85 with a spread of 4c or less. A scalp: a resting bid at the side's bid (maker, no fee) rests WAIT seconds and fills only if the side's ask comes down to it (the cautious replay of scalp.py); then a resting sell TARGET above the fill (maker, no fee) rests HOLD seconds, filling only if the bid reaches it, and not after the last 60 s of the window. A sell that does not fill is closed at the bid with the taker fee 0.07 p (1 - p) ("cut"), or held to the result ("hold", no fee). Then the next scalp starts, until 120 s remain. WAIT and HOLD are SHARES OF THE TIME LEFT at the start of each scalp (never fixed seconds); TARGET is in the market's typical minutes (rms one-second mid move times sqrt(60), from the seconds BEFORE each scalp starts; a first run used the whole window, which looks ahead, and is replaced): 0.5 or 1.0, never under 1c (a tick). No price filter inside the band.
Reported per variant: scalps, bid fill rate, sell fill rate given a fill, mean net per scalp in cents (cut and hold), net per $100 staked, scalps per market-side with a fill, net per market-side; by typical-minute size (thirds, the curve's own measure), by price band, by day. Counts are scalps; windows overlap in time. This is the maker-fill replay, which is cautious about bid fills (the ask must reach the bid) and generous about nothing else: no queue position, no slippage, no partial fills. Settings chosen after seeing the results are said to be so.
Run on the Mac: .venv/bin/python scripts/crypto_fee_free_scalps.py > studies/results/<date>-crypto-fee-free-scalps.txt"""
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
            m, ask, bid = a["m"], a["a"], a["b"]
            d2 = np.where(np.isnan(a["d2"]), 0.0, a["d2"])
            ok2 = (~np.isnan(a["d2"])).astype(float)
            cs, cn = np.cumsum(d2), np.cumsum(ok2)
            for way in ("yes", "no"):
                sa = ask if way == "yes" else 1.0 - bid
                sb = bid if way == "yes" else 1.0 - ask
                win = a["result"] == way
                k = 60
                scalps = []
                while k < 780:
                    left = 900 - k
                    if cn[k - 1] < 60 or cs[k - 1] <= 0:
                        k += 15
                        continue
                    unit = float(np.sqrt(cs[k - 1] / cn[k - 1]) * math.sqrt(60.0))  # only the seconds before the scalp starts
                    if np.isnan(sa[k]) or np.isnan(sb[k]) or not (0.60 <= sa[k] <= 0.85) or sa[k] - sb[k] > 0.04:
                        k += 15
                        continue
                    wait, hold = int(0.10 * left), int(0.25 * left)
                    pb = float(sb[k])
                    w5 = (sb[k - 300:k + 1] + sa[k - 300:k + 1]) / 2.0 if k >= 300 else None
                    if w5 is not None and not np.isnan(w5).any():
                        path = float(np.abs(np.diff(w5)).sum())
                        eff5 = abs(float(w5[-1] - w5[0])) / path if path > 0 else 1.0
                        rng5 = float(w5.max() - w5.min()) / unit
                    else:
                        eff5 = rng5 = None
                    seg = sa[k + 1:k + 1 + wait]
                    hit = np.flatnonzero(seg <= pb + 1e-9)
                    if not len(hit):
                        scalps.append((day, unit, pb, None, left, eff5, rng5))
                        k += max(15, wait)
                        continue
                    jf = k + 1 + int(hit[0])
                    out = {}
                    for tu in (0.5, 1.0):
                        tgt = max(0.01, tu * unit)
                        end = min(jf + hold, 840)
                        seg2 = sb[jf + 1:end + 1]
                        h2 = np.flatnonzero(seg2 >= pb + tgt - 1e-9)
                        if len(h2):
                            out[tu] = ("sold", pb + tgt, jf + 1 + int(h2[0]), tgt)
                        else:
                            cb = sb[end] if not np.isnan(sb[end]) else pb
                            cut = float(cb) - pb - 0.07 * float(cb) * (1 - float(cb))
                            hold_ = (1.0 if win else 0.0) - pb
                            out[tu] = ("open", cut, hold_, end)
                    scalps.append((day, unit, pb, out, left, eff5, rng5))
                    # next scalp starts after the 0.5 variant's sell or the end of its hold
                    k = jf + 1 + hold // 2
                if scalps:
                    chains.append(scalps)
alls = [s for ch in chains for s in ch]
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {len(chains)} market-sides with a scalp attempt, {len(alls)} attempts, band 0.60-0.85, whole market length")
for tu in (0.5, 1.0):
    sel = [s for s in alls if s[3] is not None]
    fills = len(sel)
    sold = [s for s in sel if s[3][tu][0] == "sold"]
    opn = [s for s in sel if s[3][tu][0] == "open"]
    gain = [s[3][tu][3] for s in sold]
    cut = [s[3][tu][1] for s in opn]
    hold = [s[3][tu][2] for s in opn]
    n_cut = sum(gain) + sum(cut)
    n_hold = sum(gain) + sum(hold)
    stake = sum(s[2] for s in sel)
    print(f"--- target {tu} typical minutes (not under 1c); wait 10% and hold 25% of the time left")
    print(f"  attempts {len(alls)} | bid filled {fills} ({100.0 * fills / len(alls):.1f}%) | sell filled {len(sold)} ({100.0 * len(sold) / max(fills, 1):.1f}% of fills) | mean target {100.0 * np.mean(gain):.2f}c")
    print(f"  unsold {len(opn)}: cut at the bid with taker fee mean {100.0 * np.mean(cut):+.2f}c, held to the result mean {100.0 * np.mean(hold):+.2f}c")
    print(f"  net per filled scalp: cut {100.0 * n_cut / fills:+.2f}c, hold {100.0 * n_hold / fills:+.2f}c | net per $100 staked: cut {100.0 * n_cut / stake:+.2f}, hold {100.0 * n_hold / stake:+.2f} (stake = the fill price)")
    print("  by the market's typical-minute size (thirds):")
    us = np.quantile([s[1] for s in sel], [1 / 3, 2 / 3])
    for nm, lo, hi in (("small", 0, us[0]), ("middle", us[0], us[1]), ("large", us[1], 9)):
        q = [s for s in sel if lo <= s[1] < hi]
        so = [s for s in q if s[3][tu][0] == "sold"]
        op = [s for s in q if s[3][tu][0] == "open"]
        nc = sum(s[3][tu][3] for s in so) + sum(s[3][tu][1] for s in op)
        nh = sum(s[3][tu][3] for s in so) + sum(s[3][tu][2] for s in op)
        print(f"    {nm:7s} unit {lo:.3f}-{min(hi, 9):.3f} filled {len(q):5d} sold {100.0 * len(so) / max(len(q), 1):4.1f}% net/scalp cut {100.0 * nc / max(len(q), 1):+.2f}c hold {100.0 * nh / max(len(q), 1):+.2f}c")
    for key, label, idx in (("eff", "path efficiency over the 5 minutes before (low = wavy, high = a straight run)", 5), ("rng", "range over the 5 minutes before, in typical minutes (high = spiky)", 6)):
        have = [s_ for s_ in sel if s_[idx] is not None]
        cs_ = np.quantile([s_[idx] for s_ in have], [1 / 3, 2 / 3])
        print(f"  by {label}, thirds (scalps that start 5 minutes or more into the window):")
        for nm, lo, hi in (("low", -9, cs_[0]), ("middle", cs_[0], cs_[1]), ("high", cs_[1], 99)):
            q = [s_ for s_ in have if lo <= s_[idx] < hi]
            so = [s_ for s_ in q if s_[3][tu][0] == "sold"]
            op = [s_ for s_ in q if s_[3][tu][0] == "open"]
            nc = sum(s_[3][tu][3] for s_ in so) + sum(s_[3][tu][1] for s_ in op)
            nh = sum(s_[3][tu][3] for s_ in so) + sum(s_[3][tu][2] for s_ in op)
            print(f"    {nm:7s} {max(lo, 0):.2f}-{min(hi, 99):.2f} filled {len(q):5d} sold {100.0 * len(so) / max(len(q), 1):4.1f}% net/scalp cut {100.0 * nc / max(len(q), 1):+.2f}c hold {100.0 * nh / max(len(q), 1):+.2f}c")
    print("  by price band at the bid:")
    for lo, hi in ((0.58, 0.68), (0.68, 0.78), (0.78, 0.86)):
        q = [s for s in sel if lo <= s[2] < hi]
        so = [s for s in q if s[3][tu][0] == "sold"]
        op = [s for s in q if s[3][tu][0] == "open"]
        nc = sum(s[3][tu][3] for s in so) + sum(s[3][tu][1] for s in op)
        nh = sum(s[3][tu][3] for s in so) + sum(s[3][tu][2] for s in op)
        print(f"    {lo:.2f}-{hi:.2f} filled {len(q):5d} sold {100.0 * len(so) / max(len(q), 1):4.1f}% net/scalp cut {100.0 * nc / max(len(q), 1):+.2f}c hold {100.0 * nh / max(len(q), 1):+.2f}c")
    print("  by day (cut | hold, cents per filled scalp):")
    for d in sorted(set(s[0] for s in sel)):
        q = [s for s in sel if s[0] == d]
        so = [s for s in q if s[3][tu][0] == "sold"]
        op = [s for s in q if s[3][tu][0] == "open"]
        nc = sum(s[3][tu][3] for s in so) + sum(s[3][tu][1] for s in op)
        nh = sum(s[3][tu][3] for s in so) + sum(s[3][tu][2] for s in op)
        print(f"    {d} filled {len(q):5d} sold {100.0 * len(so) / max(len(q), 1):4.1f}% cut {100.0 * nc / max(len(q), 1):+.2f}c | hold {100.0 * nh / max(len(q), 1):+.2f}c")
fl = [sum(1 for s in ch if s[3] is not None) for ch in chains]
print(f"--- filled scalps per market-side that had any attempt: mean {np.mean(fl):.2f}, max {max(fl)}")
