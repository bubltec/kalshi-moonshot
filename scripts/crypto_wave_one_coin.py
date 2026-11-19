"""One coin as the indicator: do the others still pay after it moves? (2026-11-19, user: "maybe any one coin could be the indicator ... not them together")

One question on the archived Kalshi quote curves (all series present, 2026-11-09 to the last archived day): at every 30 s step with 3 to 13 minutes left, TRIGGER coin A = its YES mid moved 1.0 typical minute or more over the last minute (typical minute = rms one-second mid move times sqrt(60) over the window's earlier seconds). For every OTHER coin B that has NOT yet moved (its own last-minute move under 0.3 typical minutes in the same direction), buy B's side in A's direction at B's ask at that moment and hold to settlement, fee 0.07 p (1 - p) on every leg, no price filter, net per $100 staked. One result per trigger coin (each coin alone), plus all triggers pooled, plus the same buys for the others that HAD already followed (own move 0.3 or more). The same coin's rows repeat over steps, so counts are trigger-follower pairs, not independent bets. Series are named because the question is about named indicators; a live rule must not be keyed on a name. Settings chosen after seeing the results are said to be so.
Run on the Mac: .venv/bin/python scripts/crypto_wave_one_coin.py > studies/results/<date>-crypto-wave-one-coin.txt"""
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
base = []
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
        coin = {}
        for ticker, a in tk.items():
            seen = a["d2"][~np.isnan(a["d2"])]
            if len(seen) < 300:
                continue
            u = float(np.sqrt(np.mean(seen)) * math.sqrt(60.0))
            if u > 0:
                coin[ticker.split("-")[0]] = (a, u)
        if len(coin) < 6:
            continue
        for left in range(780, 170, -30):
            k = 900 - left
            mv = {}
            for name, (a, u) in coin.items():
                if np.isnan(a["m"][[k, k - 60]]).any() or np.isnan(a["a"][k]) or np.isnan(a["b"][k]):
                    continue
                mv[name] = (a["m"][k] - a["m"][k - 60]) / u
            for A, mA in mv.items():
                if abs(mA) < 1.0:
                    continue
                s = 1.0 if mA > 0 else -1.0
                for B, mB in mv.items():
                    if B == A:
                        continue
                    a = coin[B][0]
                    ya, yb = float(a["a"][k]), float(a["b"][k])
                    if s > 0:
                        p, win = ya, a["result"] == "yes"
                    else:
                        p, win = 1.0 - yb, a["result"] == "no"
                    if not (0.02 < p < 0.98):
                        continue
                    base.append((p, win, day))
                    events.append((A, "followed" if s * mB >= 0.3 else "not yet", p, win, day, close, B, s, k))
print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {len(events)} trigger-follower pairs")


def net(rows):
    cost = sum(p + 0.07 * p * (1 - p) for p in (r[2] for r in rows))
    pay = sum(1.0 for r in rows if r[3])
    return 100.0 * (pay - cost) / cost if cost else float("nan")


def show(name, rows):
    if len(rows) < 30:
        return
    print(f"  {name:22s} pairs {len(rows):6d} | mean ask {np.mean([r[2] for r in rows]):.3f} | win {100.0 * np.mean([r[3] for r in rows]):5.1f}% | net per $100 {net(rows):+6.1f}")


print("--- dedup and baseline for the 'not yet followed' pairs (a study's addition after seeing the first run)")
nyf = [e for e in events if e[1] == "not yet"]
first = {}
for e in nyf:
    first.setdefault((e[5], e[6], e[7]), e)  # one per window, follower coin and way: the first trigger
ded = list(first.values())
show("first trigger only", ded)
lo, hi = np.quantile([e[2] for e in ded], [0.1, 0.9])
bb = [(0, 0, p, w, d) for p, w, d in base if lo <= p <= hi]
show(f"baseline same ask {lo:.2f}-{hi:.2f}", bb)
print("  by day, first trigger only (net, pairs) vs baseline in the same ask range:")
for d in sorted(set(e[4] for e in ded)):
    a_ = [e for e in ded if e[4] == d]
    b_ = [r for r in bb if r[4] == d]
    print(f"    {d} trigger {net(a_):+6.1f} ({len(a_)}) | baseline {net(b_):+6.1f} ({len(b_)})")
for state in ("not yet", "followed"):
    print(f"--- the other coin has {'NOT yet followed (own last-minute move under 0.3)' if state == 'not yet' else 'already followed (0.3 or more)'}")
    sel = [e for e in events if e[1] == state]
    show("all triggers pooled", sel)
    for A in sorted(set(e[0] for e in events)):
        show(A, [e for e in sel if e[0] == A])
