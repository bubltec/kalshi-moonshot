"""Does one coin's curve run just behind another's? (2026-11-19, user: "if you follow the other coins, hypes curve is just behind other coins like XRP")

One question on the archived Kalshi quote curves (all crypto series present, 2026-11-09 to the last archived day): for each ordered pair of series (A, B), the pooled correlation of A's 5-second YES-mid change with B's 5-second change LAG seconds later, for LAG from -60 to +60 in 5 s steps, in each coin's own typical minutes (rms one-second move of the window times sqrt(60); a study measure, not a live reading). The lag at which the pair correlates most (peak lag) says who runs behind whom: B behind A means the peak sits at a positive LAG with A as the first series. Reported for the HYPE series against each other series (the user's example, XRP first) and the pair summary for all: peak lag in seconds and the correlation there against at lag 0, and the same by day for HYPE against XRP. Then the use: after XRP's last 30 s move, HYPE's NEXT 15 s and 30 s move beyond HYPE's own last 30 s move (a regression coefficient and correlation), by day. Counts are 5-second steps, which overlap in time and across coins. Series are named because the question is about named coins; a live rule must not be keyed on a name. Settings chosen after seeing the results are said to be so.
Run on the Mac: .venv/bin/python scripts/crypto_pair_lag.py > studies/results/<date>-crypto-pair-lag.txt"""
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
        mat = {}
        for ticker, a in tk.items():
            m = a["m"]
            d2 = a["d2"][~np.isnan(a["d2"])]
            if len(d2) < 600:
                continue
            u = float(np.sqrt(np.mean(d2)) * math.sqrt(60.0))
            if u <= 0:
                continue
            ok = np.flatnonzero(~np.isnan(m))
            if len(ok) < 700:
                continue
            m = m.copy()
            m[:ok[0]] = m[ok[0]]
            x = m[::5]
            if np.isnan(x).any() or len(x) < 150:
                continue
            mat[ticker.split("-")[0]] = np.diff(x) / u
        if len(mat) >= 2:
            events.append((day, mat))
names = sorted({k for _, m in events for k in m})
LAGS = list(range(-12, 13))  # steps of 5 s


def pooled(a, b, lag, sel=None):
    xs, ys = [], []
    for d, mat in events:
        if (sel and d != sel) or a not in mat or b not in mat:
            continue
        x, y = mat[a], mat[b]
        if lag >= 0:
            xs.append(x[:len(x) - lag] if lag else x); ys.append(y[lag:])
        else:
            xs.append(x[-lag:]); ys.append(y[:len(y) + lag])
    if not xs:
        return float("nan"), 0
    xa, ya = np.concatenate(xs), np.concatenate(ys)
    return (float(np.corrcoef(xa, ya)[0, 1]) if len(xa) > 100 else float("nan")), len(xa)


print(f"window: {days[0].split('day=')[1][:10]} to {days[-1].split('day=')[1][:10]}; {len(events)} coin-windows; series: {', '.join(n.replace('15M', '').replace('KX', '') for n in names)}")
print("--- peak lag of B behind A (seconds; positive = B moves after A), corr at the peak and at 0, 5 s changes")
rows = []
for a in names:
    for b in names:
        if a >= b:
            continue
        cs = [pooled(a, b, L)[0] for L in LAGS]
        csr = np.array(cs)
        if np.all(np.isnan(csr)):
            continue
        i = int(np.nanargmax(csr))
        rows.append((a, b, LAGS[i] * 5, csr[i], cs[LAGS.index(0)], cs[LAGS.index(-1)], cs[LAGS.index(1)]))
lab = lambda s: s.replace("15M", "").replace("KX", "")  # noqa: E731
for a, b, pl, cp, c0, cm1, cp1 in sorted(rows, key=lambda r: (lab(r[0]) != "HYPE" and lab(r[1]) != "HYPE", r[0], r[1])):
    who = f"{lab(b)} behind {lab(a)} by {pl} s" if pl > 0 else (f"{lab(a)} behind {lab(b)} by {-pl} s" if pl < 0 else "together")
    print(f"  {lab(a):5s} {lab(b):5s} peak at {pl:+4d} s ({who}) | corr at peak {cp:+.3f} | at 0 {c0:+.3f} | at -5 s {cm1:+.3f} | at +5 s {cp1:+.3f}")
print("--- HYPE and XRP by lag (the correlation of XRP's change with HYPE's change LAG seconds later; negative LAG = HYPE first)")
if "KXHYPE15M" in names and "KXXRP15M" in names:
    print("  " + " ".join(f"{L * 5:+d}:{pooled('KXXRP15M', 'KXHYPE15M', L)[0]:+.3f}" for L in LAGS))
    print("--- by day, peak lag of HYPE behind XRP and the correlation there")
    for d in sorted({d for d, _ in events}):
        cs = np.array([pooled("KXXRP15M", "KXHYPE15M", L, d)[0] for L in LAGS])
        if not np.all(np.isnan(cs)):
            i = int(np.nanargmax(cs))
            print(f"  {d} peak {LAGS[i] * 5:+d} s corr {cs[i]:+.3f} at 0 {cs[LAGS.index(0)]:+.3f}")
    print("--- the use: after XRP's last 30 s move (6 steps), HYPE's next 15 s and 30 s move (typical minutes), beyond HYPE's own last 30 s")
    X, H0, H15, H30 = [], [], [], []
    for d, mat in events:
        if "KXXRP15M" in mat and "KXHYPE15M" in mat:
            x, h = mat["KXXRP15M"], mat["KXHYPE15M"]
            n = min(len(x), len(h))
            for i in range(6, n - 6):
                X.append(x[i - 6:i].sum()); H0.append(h[i - 6:i].sum()); H15.append(h[i:i + 3].sum()); H30.append(h[i:i + 6].sum())
    X, H0, H15, H30 = map(np.array, (X, H0, H15, H30))
    for nm, y in (("next 15 s", H15), ("next 30 s", H30)):
        A = np.column_stack([X, H0, np.ones(len(X))])
        coef = np.linalg.lstsq(A, y, rcond=None)[0]
        print(f"  HYPE {nm}: on XRP's last 30 s {coef[0]:+.3f}, on HYPE's own last 30 s {coef[1]:+.3f} | corr with XRP's {np.corrcoef(X, y)[0, 1]:+.3f}, with own {np.corrcoef(H0, y)[0, 1]:+.3f} | {len(X)} steps")
    gap = X - H0
    big = np.abs(gap) >= np.quantile(np.abs(gap), 0.9)
    print(f"  when XRP's last 30 s moved 1+ typical minute more than HYPE's (top 10% gap, {int(big.sum())} steps): HYPE's next 30 s move signed with the gap {np.mean(np.sign(gap[big]) * H30[big]):+.3f} (typical minutes); the same over all steps {np.mean(np.sign(gap) * H30):+.3f}")
