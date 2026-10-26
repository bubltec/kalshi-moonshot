"""THE CLASS TABLE: how much history does the 36-class shape table need? Original: How much history does a 15-minute fit need? Per coin, fitted on only the previous 1, 3 or 12 months (or everything before), scored on the next month. Read-only analysis of downloaded files.

The same baseline and model as coin_windows_study.py (decision minute 8 of the quarter-hour; look-back slices scaled by volatility). For each test month from 2023-06 on, the log-loss gain of the model over the baseline
(per 1000 windows; positive = the model helps) is computed with weights fitted on each history length; the table shows the average over the test months and its standard error across months.
    python scripts/history_length_study.py KLINE_DIR [KLINE_DIR ...]
"""
import glob
import math
import sys

import numpy as np
import pyarrow.parquet as pq

COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")
M = 8
SLICES = ((0, 3), (3, 7), (7, 15), (15, 30), (30, 60), (60, 120))


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


def fit(x, y, ridge=1.0, iters=25):
    x1 = np.hstack([np.ones((len(x), 1)), x])
    b = np.zeros(x1.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-x1 @ b))
        w = p * (1 - p) + 1e-9
        g = x1.T @ (y - p) - ridge * np.r_[0, b[1:]]
        h = (x1 * w[:, None]).T @ x1 + ridge * np.diag(np.r_[0, np.ones(len(b) - 1)])
        b = b + np.linalg.solve(h, g)
    return b


def predict(b, x):
    return 1 / (1 + np.exp(-(np.hstack([np.ones((len(x), 1)), x]) @ b)))


def logloss(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


print(f"log-loss gain of the 36-class table over the baseline per 1000 windows (decision minute {M}), table built on the previous k months only, average over test months from 2023-06, +- standard error across months")
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
    t = starts + M
    v1 = vol(t, 240)
    so_far = np.log(cl[t - 1] / op[starts])
    pb = np.clip(phi(so_far / (v1 * np.sqrt(15 - M))), 1e-4, 1 - 1e-4)
    lg = np.log(pb / (1 - pb))
    path = cl[starts[:, None] + np.arange(M)[None, :]]
    pos = (path[:, -1] - path.min(axis=1)) / np.maximum(path.max(axis=1) - path.min(axis=1), 1e-12)
    since_hi = M - 1 - path.argmax(axis=1)
    pace = np.log(cl[t - 1] / cl[t - 4])
    cls = (so_far > 0).astype(int) * 18 + np.minimum((pos * 3).astype(int), 2) * 6 + np.where(since_hi == 0, 0, np.where(since_hi <= 2, 1, 2)) * 2 + (pace > 0).astype(int)
    ok = np.isfinite(pb) & np.isfinite(pos)
    month = (ot[starts].astype("datetime64[ms]").astype("datetime64[M]")).astype(int)
    results = {k: [] for k in (1, 3, 12, 999)}
    for mth in range(int(month[ok].min()) + 12, int(month[ok].max()) + 1):
        te = ok & (month == mth)
        if te.sum() < 1500 or mth < (2023 - 1970) * 12 + 5:
            continue
        for k in results:
            tr = ok & (month < mth) & (month >= mth - k)
            if tr.sum() < 1500:
                continue
            lift = np.zeros(36)
            for c in range(36):
                sel = tr & (cls == c)
                cnt = int(sel.sum())
                if cnt >= 100:
                    d = 0.0
                    for _ in range(15):
                        pp = 1 / (1 + np.exp(-(lg[sel] + d)))
                        d += (up[sel] - pp).sum() / max((pp * (1 - pp)).sum(), 1e-9)
                    lift[c] = d * cnt / (cnt + 300)
            pt = np.clip(1 / (1 + np.exp(-(lg + lift[cls]))), 1e-4, 1 - 1e-4)
            results[k].append(1000 * (logloss(pb[te], up[te]) - logloss(pt[te], up[te])))
    line = f"{coin:5}"
    for k, name in ((1, "1 month"), (3, "3 months"), (12, "12 months"), (999, "all before")):
        v = np.array(results[k])
        line += f" | {name}: {v.mean():+5.2f} +-{v.std(ddof=1) / math.sqrt(len(v)):.2f}" if len(v) > 2 else f" | {name}: n/a"
    print(line + f"   ({len(results[1])} test months)")
