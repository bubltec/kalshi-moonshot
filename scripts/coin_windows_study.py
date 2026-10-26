"""Per coin, on years of 1-minute data: does the multi-window picture predict a 15-minute window's direction beyond the baseline? Read-only analysis of downloaded files. Places nothing.

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
        print(f"\n{coin}: no files in the folders given")
        continue
    ot, op, cl = data
    r1 = np.diff(np.log(cl), prepend=np.log(cl[0]))
    n = len(cl)
    csum, csq = np.cumsum(r1), np.cumsum(r1 * r1)

    def vol(i, w):  # standard deviation of the w returns ending at i-1
        a, b = i - w, i
        s1, s2 = csum[b - 1] - csum[a - 1], csq[b - 1] - csq[a - 1]
        return np.sqrt(np.maximum(s2 / w - (s1 / w) ** 2, 1e-14))

    starts = np.where((ot // 60000) % 15 == 0)[0]
    starts = starts[(starts + 15 < n) & (starts > 1500)]
    starts = starts[(ot[starts + 14] - ot[starts]) == 14 * 60000]  # a complete window with no gap
    up = (cl[starts + 14] > op[starts]).astype(float)
    print(f"\n{coin}: {len(starts):,} windows from {np.datetime64(int(ot[starts[0]]), 'ms')} to {np.datetime64(int(ot[starts[-1]]), 'ms')}; up {up.mean():.1%}")
    train = ot[starts] < SPLIT_MS
    for m in MINUTES:
        t = starts + m
        v1 = vol(t, 240)
        so_far = np.log(cl[t - 1] / op[starts])
        z = so_far / (v1 * np.sqrt(15 - m))
        pb = phi(z)
        feats = [logit(pb)]
        for lo, hi in SLICES:
            a, b = t - 1 - lo, t - 1 - hi
            feats.append(np.log(cl[a] / cl[b]) / (v1 * np.sqrt(hi - lo)))
        feats.append(np.log(vol(t, 60) / vol(t, 1440)))
        x = np.column_stack(feats)
        ok = np.isfinite(x).all(axis=1)
        tr, te = ok & train, ok & ~train
        if tr.sum() < 1000 or te.sum() < 1000:
            print(f"  minute {m:2}: too few windows")
            continue
        mu, sd = x[tr].mean(0), x[tr].std(0) + 1e-9
        xs = (x - mu) / sd
        b = fit_logistic(xs[tr], up[tr])
        pm = 1 / (1 + np.exp(-(np.hstack([np.ones((len(xs), 1)), xs]) @ b)))
        lo_, hi_ = np.quantile(pb[te], [0.1, 0.9])
        low, high = te & (pb <= lo_), te & (pb >= hi_)
        lb, lm = logloss(pb[te], up[te]), logloss(pm[te], up[te])
        print(f"  minute {m:2}: baseline p lowest tenth {pb[low].mean():.3f} vs up {up[low].mean():.3f}, highest tenth {pb[high].mean():.3f} vs up {up[high].mean():.3f};  log-loss baseline {lb:.4f} model {lm:.4f} ({1000 * (lb - lm):+.2f} per 1000 windows, {te.sum():,} test windows)")
        d = (pm - pb)[te]
        order = np.argsort(d)
        k = len(order) // 10
        lo_i, hi_i = order[:k], order[-k:]
        edge_lo = float((up[te][lo_i] - pb[te][lo_i]).mean())
        edge_hi = float((up[te][hi_i] - pb[te][hi_i]).mean())
        print(f"             the model moves p by {100 * np.abs(d).mean():.2f} points on average; its most-down tenth ({100 * d[lo_i].mean():+.2f} pts) realized {100 * edge_lo:+.2f} pts against the baseline, its most-up tenth ({100 * d[hi_i].mean():+.2f} pts) realized {100 * edge_hi:+.2f}")
        names = ["baseline"] + [f"{lo}-{hi}m" for lo, hi in SLICES] + ["vol ratio"]
        top = sorted(zip(names[1:], b[2:], strict=True), key=lambda p: -abs(p[1]))[:2]
        print("             biggest weights (per sd): " + ", ".join(f"{n} {w:+.3f}" for n, w in top) + f"; baseline weight {b[1]:+.3f}")
