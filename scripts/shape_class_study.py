"""SHAPE CLASSES: each move is put in one of 36 classes (ahead or behind so far x position in the window's range, in thirds x time since its high, now / 1-2 minutes / 3+ x direction of the last 3 minutes); a table of what became of past moves in each class gives the probability. Original: WAVE POSITION: where the price sits on its own curve inside the window (range position, time since its high and low, pullback from the high, bounce off the low), added to the baseline and the six slices. Does it help, per coin, on 2025 onward? Original: Per coin, on years of 1-minute data: does the multi-window picture predict a 15-minute window's direction beyond the baseline? Read-only analysis of downloaded files. Places nothing.

Data: Binance spot 1-minute candles (KEDevO/crypto-market-datasets, CC BY 4.0), one coin at a time (never pooled). A window is a UTC quarter-hour; `up` = its last close is above its first open (a Kalshi
15-minute market asks that of the index, up to the index difference). At decision minute m the model sees minutes 0..m-1 only.
Baseline (a random walk): z = log(close so far / open) / (per-minute volatility x sqrt(minutes left)); p = Phi(z). Per-minute volatility is the standard deviation of the previous 240 one-minute returns.
Model: logistic regression on logit(p) plus the six look-back slices before the decision (3, 4, 8, 15, 30, 60 minutes back, each scaled by that volatility x sqrt(length)) and the volatility ratio (last hour / last day).
Fitted on 2022-2024 and scored on 2025 onward. Reported per coin and decision minute: how the baseline is calibrated (up rate vs its p in the lowest and highest tenth of p) and the log-loss improvement of the model over the baseline.
    python scripts/coin_windows_study.py KLINE_DIR [KLINE_DIR ...]    (each holds <COIN>USDT_*.parquet or <COIN>USDT/*.parquet; needs numpy and pyarrow)
"""
import glob
import math
import os
import sys

import numpy as np
import pyarrow.parquet as pq

SPLIT_MS = 1735689600000  # 2025-01-01 UTC
COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")
MINUTES = (3, 5, 8, 10)
SLICES = ((0, 3), (3, 7), (7, 15), (15, 30), (30, 60), (60, 120))


def load(coin):
    files = []
    for d in sys.argv[1:]:
        files += glob.glob(f"{d}/{coin}USDT_*.parquet") + glob.glob(f"{d}/{coin}USDT/*.parquet")
    if not files:
        return None
    t = [pq.read_table(f, columns=["open_time", "open", "close"]) for f in sorted(files)]
    ot = np.concatenate([x["open_time"].to_numpy() for x in t]).astype(np.int64)
    op = np.concatenate([x["open"].to_numpy() for x in t]).astype(float)
    cl = np.concatenate([x["close"].to_numpy() for x in t]).astype(float)
    order = np.argsort(ot)
    ot, op, cl = ot[order], op[order], cl[order]
    keep = np.concatenate([[True], np.diff(ot) > 0])
    return ot[keep], op[keep], cl[keep]


def phi(z):
    return 0.5 * (1.0 + np.vectorize(math.erf)(z / math.sqrt(2.0)))


def logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def fit_logistic(x, y, ridge=1.0, iters=30):
    x1 = np.hstack([np.ones((len(x), 1)), x])
    b = np.zeros(x1.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-x1 @ b))
        w = p * (1 - p) + 1e-9
        g = x1.T @ (y - p) - ridge * np.r_[0, b[1:]]
        h = (x1 * w[:, None]).T @ x1 + ridge * np.diag(np.r_[0, np.ones(len(b) - 1)])
        b = b + np.linalg.solve(h, g)
    return b


def logloss(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


for coin in COINS:
    data = load(coin)
    if data is None:
        continue
    ot, op, cl = data
    r1 = np.diff(np.log(cl), prepend=np.log(cl[0]))
    n = len(cl)
    csum, csq = np.cumsum(r1), np.cumsum(r1 * r1)

    def vol(i, w):
        a, b = i - w, i
        s1, s2 = csum[b - 1] - csum[a - 1], csq[b - 1] - csq[a - 1]
        return np.sqrt(np.maximum(s2 / w - (s1 / w) ** 2, 1e-14))

    starts = np.where((ot // 60000) % 15 == 0)[0]
    starts = starts[(starts + 15 < n) & (starts > 1500)]
    starts = starts[(ot[starts + 14] - ot[starts]) == 14 * 60000]
    up = (cl[starts + 14] > op[starts]).astype(float)
    train = ot[starts] < SPLIT_MS
    print(f"\n{coin}: {len(starts):,} windows")
    for m in (5, 8, 10):
        t = starts + m
        v1 = vol(t, 240)
        so_far = np.log(cl[t - 1] / op[starts])
        pb = np.clip(phi(so_far / (v1 * np.sqrt(15 - m))), 1e-4, 1 - 1e-4)
        idx = starts[:, None] + np.arange(m)[None, :]
        path = cl[idx]
        hi_, lo_ = path.max(axis=1), path.min(axis=1)
        pos = (path[:, -1] - lo_) / np.maximum(hi_ - lo_, 1e-12)
        since_hi = m - 1 - path.argmax(axis=1)
        pace = np.log(cl[t - 1] / cl[t - 4])
        cls = ((so_far > 0).astype(int) * 18 + np.minimum((pos * 3).astype(int), 2) * 6 + np.where(since_hi == 0, 0, np.where(since_hi <= 2, 1, 2)) * 2 + (pace > 0).astype(int))
        ok = np.isfinite(pb) & np.isfinite(pos)
        tr, te = ok & train, ok & ~train
        lift = np.zeros(36)  # per class, a shift in log-odds: the value that makes the class's average predicted p match what happened
        cnt = np.zeros(36, int)
        lg = np.log(pb / (1 - pb))
        for c in range(36):
            sel = tr & (cls == c)
            cnt[c] = sel.sum()
            if cnt[c] >= 200:
                d = 0.0
                for _ in range(15):
                    pp = 1 / (1 + np.exp(-(lg[sel] + d)))
                    d += (up[sel] - pp).sum() / max((pp * (1 - pp)).sum(), 1e-9)
                lift[c] = d * cnt[c] / (cnt[c] + 300)  # shrunk toward zero when a class is thin
        pt = np.clip(1 / (1 + np.exp(-(lg + lift[cls]))), 1e-4, 1 - 1e-4)
        x = np.column_stack([np.log(pb / (1 - pb)), pos, np.minimum(since_hi, 4), pace / (v1 * np.sqrt(3)), np.log(cl[t - 1] / cl[t - 4]) / v1])
        mu, sd = x[tr].mean(0), x[tr].std(0) + 1e-9
        b = fit_logistic(((x - mu) / sd)[tr], up[tr])
        pl = 1 / (1 + np.exp(-(np.hstack([np.ones((len(x), 1)), (x - mu) / sd]) @ b)))
        lb, lt, ll_ = logloss(pb[te], up[te]), logloss(pt[te], up[te]), logloss(pl[te], up[te])
        print(f"  minute {m:2}: log-loss gain per 1000 windows: class table {1000 * (lb - lt):+5.2f}, smooth model on the same inputs {1000 * (lb - ll_):+5.2f}  ({te.sum():,} test windows; {int((cnt >= 200).sum())} of 36 classes have 200+ training windows)")
        if m == 8 and coin in ("BTC", "XRP"):
            names = {0: "at its high", 1: "high 1-2 min ago", 2: "high 3+ min ago"}
            print("             class (ahead/behind, range third, time since high, last-3-min direction): realized difference from the baseline (points), test windows")
            rows = []
            for c in range(36):
                sel = te & (cls == c)
                if sel.sum() >= 300:
                    ahead, rest = divmod(c, 18)
                    third, rest = divmod(rest, 6)
                    sh, dr = divmod(rest, 2)
                    rows.append((100 * (up[sel] - pb[sel]).mean(), f"{'ahead ' if ahead else 'behind'} {['low','mid','top'][third]} third, {names[sh]}, {'rising' if dr else 'falling'}: up {100 * up[sel].mean():5.1f}% vs {100 * pb[sel].mean():5.1f}% ({sel.sum():,})"))
            for lift_, text in sorted(rows)[:3] + sorted(rows)[-3:]:
                print(f"               {lift_:+5.1f} pts  {text}")
