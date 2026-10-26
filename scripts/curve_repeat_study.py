"""Does the curve repeat? Whole-path analogs: for a window at minute m, the closest past windows by the full minute-by-minute path so far, and what became of them. Read-only analysis of downloaded files. Places nothing.

Per coin (never pooled), at decision minute m (market time) the path is the m closes since the window opened, each as its distance from the target (the open) in units of the coin's usual per-minute move: x_k = log(close_k / open) / v1,
so the last value is the proximity to the target. The memory is the 2022-2024 windows of the same coin at the same minute; the test windows are 2025 onward. The analog chance = the share of the K closest past paths that finished above their open.
Compared by log-loss against the random-walk baseline and against the baseline blended with the analog chance (half and half in log-odds). Also: does the curve repeat at all (how close the nearest past path is, against a random one),
and where the analogs help most: by proximity to the target (|z| of the last close) and by minute.
    python scripts/curve_repeat_study.py KLINE_DIR [KLINE_DIR ...]      (needs numpy and pyarrow)
"""
import glob
import math
import sys

import numpy as np
import pyarrow.parquet as pq

SPLIT_MS = 1735689600000
COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")
MINUTES = (5, 8, 10)
K = 200


def load(coin):
    files = []
    for d in sys.argv[1:]:
        files += glob.glob(f"{d}/{coin}USDT_*.parquet") + glob.glob(f"{d}/{coin}USDT/*.parquet")
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


def logloss(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


for coin in COINS:
    ot, op, cl = load(coin)
    r1 = np.diff(np.log(cl), prepend=np.log(cl[0]))
    n = len(cl)
    csum, csq = np.cumsum(r1), np.cumsum(r1 * r1)

    def vol(i, w, csum=csum, csq=csq):
        a, b = i - w, i
        s1, s2 = csum[b - 1] - csum[a - 1], csq[b - 1] - csq[a - 1]
        return np.sqrt(np.maximum(s2 / w - (s1 / w) ** 2, 1e-14))

    starts = np.where((ot // 60000) % 15 == 0)[0]
    starts = starts[(starts + 15 < n) & (starts > 1500)]
    starts = starts[(ot[starts + 14] - ot[starts]) == 14 * 60000]
    up = (cl[starts + 14] > op[starts]).astype(float)
    train = ot[starts] < SPLIT_MS
    print(f"\n{coin}: {len(starts):,} windows ({int(train.sum()):,} in the memory, {int((~train).sum()):,} to test)")
    for m in MINUTES:
        t = starts + m
        v1 = vol(t, 240)
        path = np.log(cl[starts[:, None] + np.arange(m)[None, :]] / op[starts][:, None]) / v1[:, None]
        so_far = path[:, -1]
        pb = np.clip(phi(so_far / np.sqrt(15 - m)), 1e-4, 1 - 1e-4)
        ok = np.isfinite(path).all(axis=1)
        tr_idx, te_idx = np.where(ok & train)[0], np.where(ok & ~train)[0][::4]
        mem, memy = path[tr_idx].astype(np.float32), up[tr_idx]
        mem2 = (mem ** 2).sum(1)
        pk = np.zeros(len(te_idx))
        nearest = np.zeros(len(te_idx))
        for a in range(0, len(te_idx), 800):
            q = path[te_idx[a:a + 800]].astype(np.float32)
            d = (q ** 2).sum(1)[:, None] + mem2[None, :] - 2 * q @ mem.T
            idx = np.argpartition(d, K, axis=1)[:, :K]
            pk[a:a + 800] = memy[idx].mean(axis=1)
            nearest[a:a + 800] = np.sqrt(np.maximum(np.take_along_axis(d, idx, axis=1).min(axis=1), 0))
        rnd = np.sqrt(np.maximum(((path[te_idx[:2000], None, :] - path[np.random.default_rng(0).choice(tr_idx, 50)][None, :, :]) ** 2).sum(2), 0)).mean()
        y, p0 = up[te_idx], pb[te_idx]
        blend = 1 / (1 + np.exp(-0.5 * (logit(p0) + logit((pk * K + 0.5 * 2) / (K + 2)))))
        lb, lk, lbl = logloss(p0, y), logloss((pk * K + 1) / (K + 2), y), logloss(blend, y)
        print(f"  minute {m:2}: nearest past path is {nearest.mean():.2f} away on average, a random one {rnd:.2f}; log-loss baseline {lb:.4f}, analogs alone {lk:.4f}, baseline+analogs {lbl:.4f}  (gain over baseline per 1000: analogs {1000 * (lb - lk):+.2f}, blend {1000 * (lb - lbl):+.2f}; {len(te_idx):,} test windows)")
        if m == 8:
            z = np.abs(so_far[te_idx])
            for lo, hi, name in ((0, 0.5, "within 0.5 of the target"), (0.5, 1.5, "0.5-1.5 away"), (1.5, 99, "more than 1.5 away")):
                sel = (z >= lo) & (z < hi)
                if sel.sum() > 300:
                    print(f"             {name:24} n={int(sel.sum()):5}: blend gain {1000 * (logloss(p0[sel], y[sel]) - logloss(blend[sel], y[sel])):+6.2f} per 1000; baseline {p0[sel].mean():.3f} vs up {y[sel].mean():.3f}; analogs say {pk[sel].mean():.3f}")
