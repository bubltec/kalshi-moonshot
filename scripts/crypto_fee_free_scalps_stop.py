"""Fee-free scalps with a stop. (2026-11-19, user: "Test with a stop")

The same replay as crypto_fee_free_scalps.py (resting bid at the side's bid in the 0.60 to 0.85 band, fills only if the side's ask comes down to it, wait 10% of the time left; resting sell TARGET above the fill, hold 25% of the time left, repeated per market and coin from minute 1 to 120 s left, maker legs no fee, typical minute from the seconds before each scalp), with a STOP on the open leg: if the side's bid falls to the fill less STOP before the sell fills, the position is sold at that second's bid with the taker fee 0.07 p (1 - p). TARGET and STOP are in typical minutes (not under 1c), each 0.5 or 1.0, and 'no stop' as the reference. A sell that neither hit target nor stop by the end of the hold is cut at the bid with the fee, or held to the result. If target and stop are both inside the same second the stop counts (cautious). The fill rule, sizes and bands are unchanged; no queue position, no slippage beyond the second's bid.
Reported per target and stop pair: filled scalps, how they ended (sold at target, stopped, unsold), mean net per filled scalp in cents (cut, hold), net per $100 staked; then by the typical-minute size, the 5-minute path efficiency (low = wavy) and the 5-minute range in typical minutes (high = spiky), thirds, and by day for each. Settings chosen after seeing the results are said to be so.
Run on the Mac: .venv/bin/python scripts/crypto_fee_free_scalps_stop.py > studies/results/<date>-crypto-fee-free-scalps-stop.txt"""
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
                win = a["result"] == way
                k = 60
                while k < 780:
                    left = 900 - k
                    if cn[k - 1] < 60 or cs[k - 1] <= 0 or np.isnan(sa[k]) or np.isnan(sb[k]) or not (0.60 <= sa[k] <= 0.85) or sa[k] - sb[k] > 0.04:
                        k += 15
                        continue
                    unit = float(np.sqrt(cs[k - 1] / cn[k - 1]) * math.sqrt(60.0))
                    wait, hold = int(0.10 * left), int(0.25 * left)
                    pb = float(sb[k])
                    w5 = (sb[k - 300:k + 1] + sa[k - 300:k + 1]) / 2.0 if k >= 300 else None
                    if w5 is not None and not np.isnan(w5).any():
                        path = float(np.abs(np.diff(w5)).sum())
                        eff5 = abs(float(w5[-1] - w5[0])) / path if path > 0 else 1.0
                        rng5 = float(w5.max() - w5.min()) / unit
                    else:
                        eff5 = rng5 = None
                    sm = (sa + sb) / 2.0
                    if k >= 300 and not np.isnan(sm[k - 300:k + 1]).any():
                        slope3 = float(sm[k] - sm[k - 180]) / unit
                        dd5 = float(sm[k - 300:k + 1].max() - sm[k]) / unit
                        slope30 = float(sm[k] - sm[k - 30]) / unit
                    else:
                        slope3 = dd5 = slope30 = None
                    hit = np.flatnonzero(sa[k + 1:k + 1 + wait] <= pb + 1e-9)
                    if not len(hit):
                        k += max(15, wait)
                        continue
                    jf = k + 1 + int(hit[0])
                    end = min(jf + hold, 840)
                    seg = sb[jf + 1:end + 1]
                    outs = {}
                    for tu in (0.5, 1.0, 1.5):
                        tgt = max(0.01, tu * unit)
                        h_t = np.flatnonzero(seg >= pb + tgt - 1e-9)
                        t_t = int(h_t[0]) if len(h_t) else 10 ** 9
                        for su in (None, 1.0):
                            if su is None:
                                t_s = 10 ** 9
                            else:
                                h_s = np.flatnonzero(seg <= pb - max(0.01, su * unit) + 1e-9)
                                t_s = int(h_s[0]) if len(h_s) else 10 ** 9
                            if t_s <= t_t and t_s < 10 ** 9:
                                sp = float(seg[t_s])
                                outs[(tu, su)] = ("stop", sp - pb - 0.07 * sp * (1 - sp), None)
                            elif t_t < 10 ** 9:
                                outs[(tu, su)] = ("sold", tgt, None)
                            else:
                                cb = float(sb[end]) if not np.isnan(sb[end]) else pb
                                outs[(tu, su)] = ("open", cb - pb - 0.07 * cb * (1 - cb), (1.0 if win else 0.0) - pb)
                    rows.append((day, unit, pb, eff5, rng5, outs, slope3, dd5, slope30))
                    k = jf + 1 + hold // 2
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {len(rows)} filled scalps, band 0.60-0.85, whole market length")
KEYS = [(tu, su) for tu in (0.5, 1.0, 1.5) for su in (None, 1.0)]


def nets(sel, key):
    tot_c = tot_h = 0.0
    for r in sel:
        o = r[5][key]
        if o[0] == "open":
            tot_c += o[1]; tot_h += o[2]
        else:
            tot_c += o[1]; tot_h += o[1]
    n = max(len(sel), 1)
    return 100.0 * tot_c / n, 100.0 * tot_h / n


def thirds(idx):
    have = [r for r in rows if r[idx] is not None]
    cuts = np.quantile([r[idx] for r in have], [1 / 3, 2 / 3])
    return [(nm, [r for r in have if lo <= r[idx] < hi], lo, hi) for nm, lo, hi in (("low", -9, cuts[0]), ("middle", cuts[0], cuts[1]), ("high", cuts[1], 99))]


print("--- all filled scalps: target / stop (typical minutes) | sold at target, stopped, unsold | net per filled scalp in cents, cut and hold | net per $100 staked (cut)")
stake = sum(r[2] for r in rows)
for key in KEYS:
    c = collections.Counter(r[5][key][0] for r in rows)
    nc, nh = nets(rows, key)
    print(f"  target {key[0]} stop {str(key[1]):4s} | sold {100.0 * c['sold'] / len(rows):4.1f}% stopped {100.0 * c['stop'] / len(rows):4.1f}% unsold {100.0 * c['open'] / len(rows):4.1f}% | {nc:+.2f}c {nh:+.2f}c | {nc * len(rows) / stake:+.2f}")
for label, idx in (("typical-minute size", 1), ("5-minute path efficiency (low = wavy)", 3), ("5-minute range in typical minutes (high = spiky)", 4)):
    print(f"--- by {label}, thirds, net per filled scalp in cents (cut)")
    print("  " + " " * 8 + "".join(f"  t{k[0]}/s{k[1]}"[:12].rjust(12) for k in KEYS))
    for nm, sel, lo, hi in thirds(idx):
        print(f"  {nm:7s} n={len(sel):5d}" + "".join(f"{nets(sel, k)[0]:+12.2f}" for k in KEYS))
print("--- by day, net per filled scalp in cents (cut)")
for d in sorted(set(r[0] for r in rows)):
    sel = [r for r in rows if r[0] == d]
    print(f"  {d} n={len(sel):5d}" + "".join(f"{nets(sel, k)[0]:+12.2f}" for k in KEYS))

print("--- by the SETUP at the buy (the user's own: a trend up caught at 0.60 to 0.75; a dip that has levelled out; a fall still running; the rest), net per filled scalp in cents (cut), scalps that start 5 minutes or more into the window")
have = [r for r in rows if r[6] is not None]
setups = (
    ("rising: 3-min move up 1.0+ unit, bid 0.60-0.75", lambda r: r[6] >= 1.0 and r[2] <= 0.75),
    ("dip levelling: 1.0+ unit under the 5-min high, last 30 s not falling", lambda r: r[7] >= 1.0 and r[8] >= 0 and r[6] > -1.0),
    ("falling: 3-min move down 1.0+ unit", lambda r: r[6] <= -1.0),
    ("flat: |3-min move| under 1.0, not 1.0 under the high", lambda r: abs(r[6]) < 1.0 and r[7] < 1.0),
)
print("  " + " " * 70 + "".join(f"  t{k[0]}/s{k[1]}"[:12].rjust(12) for k in KEYS))
for nm, f in setups:
    sel = [r for r in have if f(r)]
    if len(sel) >= 30:
        c = collections.Counter(r[5][(1.0, None)][0] for r in sel)
        print(f"  {nm:70s}" + "".join(f"{nets(sel, k)[0]:+12.2f}" for k in KEYS) + f"   n={len(sel)}, sold at 1.0u {100.0 * c['sold'] / len(sel):.0f}%")
print("--- by day for the two setups the user named, target 1.0 no stop / target 1.5 no stop (cents)")
for nm, f in setups[:2]:
    print("  " + nm)
    for d in sorted(set(r[0] for r in have)):
        sel = [r for r in have if r[0] == d and f(r)]
        if len(sel) >= 20:
            print(f"    {d} n={len(sel):4d} {nets(sel, (1.0, None))[0]:+6.2f} {nets(sel, (1.5, None))[0]:+6.2f}")
