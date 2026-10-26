"""THE HOUR AROUND THE TARGET: how often and how far the last hour's price has been on each side of this window's target (its opening price), added to the slices and the wave position. Original: WAVE POSITION: where the price sits on its own curve inside the window (range position, time since its high and low, pullback from the high, bounce off the low), added to the baseline and the six slices. Does it help, per coin, on 2025 onward? Original: Per coin, on years of 1-minute data: does the multi-window picture predict a 15-minute window's direction beyond the baseline? Read-only analysis of downloaded files. Places nothing.

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
        base = [np.log(pb / (1 - pb))]
        sl = []
        for lo, hi in SLICES:
            sl.append(np.log(cl[t - 1 - lo] / cl[t - 1 - hi]) / (v1 * np.sqrt(hi - lo)))
        sl.append(np.log(vol(t, 60) / vol(t, 1440)))
        idx = starts[:, None] + np.arange(m)[None, :]
        path = cl[idx]
        hi_, lo_ = path.max(axis=1), path.min(axis=1)
        pos = (path[:, -1] - lo_) / np.maximum(hi_ - lo_, 1e-12)
        since_hi = (m - 1 - path.argmax(axis=1)) / m
        pull = np.log(hi_ / path[:, -1]) / (v1 * np.sqrt(m))
        bounce = np.log(path[:, -1] / lo_) / (v1 * np.sqrt(m))
        wave = [pos, since_hi, (m - 1 - path.argmin(axis=1)) / m, pull, bounce]
        # the last hour (60 closes before the decision) against THIS window's target (its open)
        hour = cl[(t - 60)[:, None] + np.arange(60)[None, :]]
        rel = np.log(hour / op[starts][:, None])
        above = rel > 0
        frac_above = above.mean(axis=1)
        crossings = (above[:, 1:] != above[:, :-1]).sum(axis=1)
        max_above = np.maximum(rel.max(axis=1), 0) / (v1 * np.sqrt(60))
        max_below = np.maximum(-rel.min(axis=1), 0) / (v1 * np.sqrt(60))
        last_cross = np.where(crossings > 0, 59 - np.array([np.max(np.where(r[1:] != r[:-1])[0]) if (r[1:] != r[:-1]).any() else 0 for r in above]), 60) / 60.0
        target_hr = [frac_above, np.minimum(crossings, 12) / 12.0, max_above, max_below, last_cross]
        sets = {"slices + wave": np.column_stack(base + sl + wave), "slices + wave + hour around the target": np.column_stack(base + sl + wave + target_hr), "baseline + hour around the target only": np.column_stack(base + target_hr)}
        ok = np.isfinite(sets["slices + wave + hour around the target"]).all(axis=1)
        tr, te = ok & train, ok & ~train
        ll, bw = {}, None
        for name, x in sets.items():
            mu, sd = x[tr].mean(0), x[tr].std(0) + 1e-9
            xs = (x - mu) / sd
            b = fit_logistic(xs[tr], up[tr])
            pm = 1 / (1 + np.exp(-(np.hstack([np.ones((len(xs), 1)), xs]) @ b)))
            ll[name] = logloss(pm[te], up[te])
            if name == "slices + wave + hour around the target":
                bw = b
        lb = logloss(pb[te], up[te])
        print(f"  minute {m:2}: gain over the baseline per 1000 windows: slices + wave {1000 * (lb - ll['slices + wave']):+5.2f}, + the hour around the target {1000 * (lb - ll['slices + wave + hour around the target']):+5.2f}, hour-around-target only {1000 * (lb - ll['baseline + hour around the target only']):+5.2f}")
        names = ["time above", "crossings", "highest above", "lowest below", "since last cross"]
        wts = sorted(zip(names, bw[-5:], strict=True), key=lambda p: -abs(p[1]))[:3]
        print("             hour-around-target weights (per sd): " + ", ".join(f"{k} {w:+.3f}" for k, w in wts))
