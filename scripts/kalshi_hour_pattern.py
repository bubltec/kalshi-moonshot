"""Does the last hour's pattern, and the day and week trend, change the odds on top of Kalshi's own price? Kalshi's 1-minute candles and results only. Read-only analysis of pulled files. Places nothing.

Per coin, markets in time order. For each market and decision minute m (5, 8, 10), with the YES mid at the end of minute m:
  the hour pattern  the results of the previous 4 windows (the "Past" arrows in the app), as: how many were up, and whether the last one was up (only when the 4 windows really are the 4 before: no gap)
  the day trend     the share of the previous 96 windows (24 hours) that finished up, minus 0.5;  the week trend: the previous 672 windows
  the shape         where the mid sits in its own range so far, how long ago its high was, and the direction of the last 3 minutes (as in kalshi_vector_profit.py)
Four forecasts of the result are compared by log-loss on the later 40% of the markets (fitted on the earlier 60%): Kalshi's mid alone; the mid + the shape; the mid + the hour pattern and trends; all of it.
The gain is per 1000 markets (positive = better than the mid alone). Then a buy rule: where the full model's chance beats the side's ask by a bar (first such minute), return with the buy fee, held to the result.
    python scripts/kalshi_hour_pattern.py KALSHI_DIR            (needs numpy)
"""
import glob
import json
import math
import os
import sys

import numpy as np

KD = sys.argv[1]
MINUTES = (5, 8, 10)


def fee(p):
    return 0.07 * p * (1 - p)


def fit(x, y, ridge=3.0, iters=30):
    x1 = np.hstack([np.ones((len(x), 1)), x])
    b = np.zeros(x1.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-x1 @ b))
        w = p * (1 - p) + 1e-9
        g = x1.T @ (y - p) - ridge * np.r_[0, b[1:]]
        h = (x1 * w[:, None]).T @ x1 + ridge * np.diag(np.r_[0, np.ones(len(b) - 1)])
        b = b + np.linalg.solve(h, g)
    return b


def pred(b, x):
    return 1 / (1 + np.exp(-(np.hstack([np.ones((len(x), 1)), x]) @ b)))


def logloss(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


for path in sorted(glob.glob(os.path.join(KD, "*.jsonl"))):
    series = os.path.basename(path)[:-6]
    mk = [json.loads(line) for line in open(path)]
    mk = [m for m in mk if len(m["candles"]) >= 10]
    mk.sort(key=lambda m: m["close_ts"])
    if len(mk) < 1500:
        print(f"\n{series}: only {len(mk)} markets so far")
        continue
    res = np.array([1.0 if m["result"] == "yes" else 0.0 for m in mk])
    closes = np.array([m["close_ts"] for m in mk])
    rows = []
    for i, m in enumerate(mk):
        if i < 700 or closes[i] - closes[i - 4] != 3600 or closes[i] - closes[i - 672] != 672 * 900:
            continue  # the 4 windows before, and the week before, must be unbroken
        by = {c["t"]: c for c in m["candles"]}
        ups4 = res[i - 4:i].sum()
        last = res[i - 1]
        day = res[i - 96:i].mean() - 0.5
        week = res[i - 672:i].mean() - 0.5
        mids = []
        for k in range(1, 12):
            c = by.get(m["open_ts"] + 60 * k)
            try:
                bc, ac = float(c["bc"]), float(c["ac"])
            except (TypeError, ValueError, KeyError):
                bc = ac = None
            mids.append((bc + ac) / 2 if bc and ac and 0 < bc < ac < 1 and ac - bc <= 0.08 else None)
        for mnt in MINUTES:
            path_m = mids[:mnt]
            if None in path_m:
                continue
            c = by.get(m["open_ts"] + 60 * mnt)
            hi, lo, lastm = max(path_m), min(path_m), path_m[-1]
            pos = (lastm - lo) / (hi - lo) if hi > lo else 0.5
            since = mnt - 1 - path_m.index(hi)
            rows.append({"i": i, "m": mnt, "y": res[i], "mid": lastm, "bid": float(c["bc"]), "ask": float(c["ac"]), "pos": pos, "since0": float(since == 0), "since3": float(since >= 3), "rise": float(lastm > path_m[-4]),
                         "ups4": ups4 / 4, "last": last, "day": day, "week": week, "dev": day - week})
    cut = int(len(mk) * 0.6)
    print(f"\n{series}: {len(mk)} markets, {len(rows)} market-minutes; later 40% starts at market {cut}")
    for mnt in MINUTES:
        r = [x for x in rows if x["m"] == mnt]
        tr, te = [x for x in r if x["i"] < cut], [x for x in r if x["i"] >= cut]
        if len(tr) < 500 or len(te) < 300:
            continue
        col = lambda rs, keys: np.column_stack([[x[k] for x in rs] for k in keys])  # noqa: E731
        ytr, yte = np.array([x["y"] for x in tr]), np.array([x["y"] for x in te])
        lm_tr, lm_te = logit(np.array([x["mid"] for x in tr])), logit(np.array([x["mid"] for x in te]))
        shape_k, hour_k = ("pos", "since0", "since3", "rise"), ("ups4", "last", "day", "week", "dev")
        out = {}
        for name, keys in (("mid + shape", shape_k), ("mid + hour pattern and trends", hour_k), ("all", shape_k + hour_k)):
            xtr = np.column_stack([lm_tr, col(tr, keys)])
            xte = np.column_stack([lm_te, col(te, keys)])
            mu, sd = xtr.mean(0), xtr.std(0) + 1e-9
            b = fit((xtr - mu) / sd, ytr)
            p = pred(b, (xte - mu) / sd)
            out[name] = (logloss(1 / (1 + np.exp(-lm_te)), yte) - logloss(p, yte), b, p)
        print(f"  minute {mnt:2}: gain per 1000 over the mid alone: " + ", ".join(f"{k} {1000 * v[0]:+.2f}" for k, v in out.items()) + f"   ({len(te)} test market-minutes)")
        if mnt == 8:
            b = out["mid + hour pattern and trends"][1]
            print("             hour-pattern weights (per sd): " + ", ".join(f"{k} {w:+.3f}" for k, w in zip(hour_k, b[2:], strict=True)))
    # a buy rule with the full model at minute 8
    r = [x for x in rows if x["m"] == 8]
    tr, te = [x for x in r if x["i"] < cut], [x for x in r if x["i"] >= cut]
    if len(tr) >= 500 and len(te) >= 300:
        keys = ("pos", "since0", "since3", "rise", "ups4", "last", "day", "week", "dev")
        xtr = np.column_stack([logit(np.array([x["mid"] for x in tr])), np.column_stack([[x[k] for x in tr] for k in keys])])
        xte = np.column_stack([logit(np.array([x["mid"] for x in te])), np.column_stack([[x[k] for x in te] for k in keys])])
        mu, sd = xtr.mean(0), xtr.std(0) + 1e-9
        b = fit((xtr - mu) / sd, np.array([x["y"] for x in tr]))
        p = pred(b, (xte - mu) / sd)
        for bar in (0.02, 0.03, 0.05):
            got = []
            for x, pp in zip(te, p, strict=True):
                e_yes, e_no = pp - x["ask"], (1 - pp) - (1 - x["bid"])
                if max(e_yes, e_no) >= bar:
                    price, win = (x["ask"], x["y"]) if e_yes >= e_no else (1 - x["bid"], 1 - x["y"])
                    c = price + fee(price)
                    got.append(((win - c) / c, (win - price) / price))
            if len(got) >= 30:
                a = np.array(got)
                print(f"  buy rule, bar {bar:.2f}: {len(a):4} trades, return with fee {a[:, 0].mean():+6.1%} +-{100 * a[:, 0].std() / math.sqrt(len(a)):.1f}, fee-free {a[:, 1].mean():+6.1%}")
