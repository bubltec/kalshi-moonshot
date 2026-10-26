"""All the curve ideas in one memory: does a market that looks like past markets, at every scale and in Kalshi's own price, end like them? Read-only analysis of pulled files. Places nothing.

Per coin (never pooled), at decision minute m (market time) a vector point is built from three curves:
  the coin     its minute closes since the window opened, each as the distance from the target (the open) in the coin's usual per-minute move (so the last value is the proximity to the target)
  the context  points of the hour and three hours before, as the distance from the target (scaled by the square root of the minutes back)
  Kalshi       the YES mid at the end of each minute so far (and the latest bid and ask for the trade)
Each part has a weight in the distance. The K closest past markets (the earlier 60%, same coin, same minute) give: the share that finished up, and what a buy at the ask would have earned held to the result and as a scalp (a resting sell
`target` above, filled if a later minute's highest bid reaches it within 3 minutes, else hold). The later 40% are scored: log-loss against Kalshi's own mid, and a buy rule (the analog chance beats the side's ask by a bar; first such minute).
Variants: Kalshi curve only; coin curve only; coin + context; everything. Standard errors across markets.
    python scripts/analog_memory_test.py COINBASE_DIR KALSHI_DIR        (needs numpy)
"""
import csv
import json
import math
import os
import sys

import numpy as np

CB, KD = sys.argv[1], sys.argv[2]
COINS = {"KXBTC15M": "BTC", "KXETH15M": "ETH", "KXSOL15M": "SOL", "KXXRP15M": "XRP", "KXDOGE15M": "DOGE"}
MINUTES = (5, 8, 10)
K = 100
TARGET = 0.03
HOUR = (5, 10, 15, 20, 30, 45, 60)
THREE = (90, 120, 150, 180)


def fee(p):
    return 0.07 * p * (1 - p)


def logit(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def logloss(p, y):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def load_cb(coin):
    rows = {}
    for r in csv.DictReader(open(os.path.join(CB, f"{coin}.csv"))):
        rows[int(r["time"])] = (float(r["open"]), float(r["close"]))
    t0, t1 = min(rows), max(rows)
    n = (t1 - t0) // 60 + 1
    close = np.full(n, np.nan)
    openp = np.full(n, np.nan)
    for t, (o, c) in rows.items():
        close[(t - t0) // 60] = c
        openp[(t - t0) // 60] = o
    last = np.nan
    for i in range(n):  # forward-fill minutes with no trades
        if np.isnan(close[i]):
            close[i] = last
            openp[i] = last
        else:
            last = close[i]
    return t0, openp, close


def per_minute_vol(closes):
    r = np.diff(np.log(closes))
    return float(np.sqrt(max(r.var(), 1e-14))) if len(r) > 30 else None


for series, coin in COINS.items():
    path = os.path.join(KD, f"{series}.jsonl")
    if not os.path.exists(path):
        continue
    t0, cop, ccl = load_cb(coin)

    def px(t):  # the coin's close of the minute ending at time t
        i = (t - 60 - t0) // 60
        return ccl[i] if 0 <= i < len(ccl) and not np.isnan(ccl[i]) else None

    mk = [json.loads(line) for line in open(path)]
    mk = [m for m in mk if len(m["candles"]) >= 12]
    mk.sort(key=lambda m: m["close_ts"])
    if len(mk) < 2500:
        print(f"\n{coin}: only {len(mk)} markets so far")
        continue
    data = {m_: [] for m_ in MINUTES}
    for idx, m in enumerate(mk):
        o = m["open_ts"]
        by = {c["t"]: c for c in m["candles"]}
        openv = px(o + 60)
        if openv is None:
            continue
        # the open price is the open of the first minute
        i0 = (o - t0) // 60
        openv = cop[i0] if 0 <= i0 < len(cop) and not np.isnan(cop[i0]) else openv
        before = [px(o + 60 * k) for k in range(-239, 1)]
        if None in before:
            continue
        v1 = per_minute_vol(np.array(before, dtype=float))
        if not v1:
            continue
        res = 1.0 if m["result"] == "yes" else 0.0
        for mnt in MINUTES:
            closes = [px(o + 60 * k) for k in range(1, mnt + 1)]
            if None in closes:
                continue
            coin_path = np.log(np.array(closes) / openv) / v1
            ctx = []
            for j in HOUR + THREE:
                p = px(o - 60 * j + 60)
                if p is None:
                    break
                ctx.append(math.log(p / openv) / (v1 * math.sqrt(j + mnt)))
            if len(ctx) != len(HOUR + THREE):
                continue
            cs = [by.get(o + 60 * k) for k in range(1, mnt + 1)]
            if any(c is None for c in cs):
                continue
            try:
                bids = [float(c["bc"]) for c in cs]
                asks = [float(c["ac"]) for c in cs]
            except (TypeError, ValueError):
                continue
            if any(not (0 < b < a < 1) or a - b > 0.1 for b, a in zip(bids, asks, strict=True)):
                continue
            kal = [(b + a) / 2 for b, a in zip(bids, asks, strict=True)]
            later = [by.get(o + 60 * k) for k in range(mnt + 1, mnt + 4)]
            hi_bid_yes = [float(c["bh"]) for c in later if c and c.get("bh") not in (None, "")]
            lo_ask_yes = [float(c["al"]) for c in later if c and c.get("al") not in (None, "")]
            z_last = coin_path[-1]
            data[mnt].append({"i": idx, "vec_coin": coin_path, "vec_ctx": np.array(ctx), "vec_kal": np.array(kal), "y": res, "bid": bids[-1], "ask": asks[-1], "mid": kal[-1],
                              "hi_bid": max(hi_bid_yes) if hi_bid_yes else None, "lo_ask": min(lo_ask_yes) if lo_ask_yes else None, "z": z_last, "tk": m["ticker"]})
    print(f"\n{coin}: {len(mk)} markets")
    cut = int(len(mk) * 0.6)
    for mnt in MINUTES:
        rows = data[mnt]
        tr, te = [r for r in rows if r["i"] < cut], [r for r in rows if r["i"] >= cut]
        if len(tr) < 1500 or len(te) < 500:
            continue
        y = np.array([r["y"] for r in te])
        mid = np.array([r["mid"] for r in te])
        ytr = np.array([r["y"] for r in tr])
        base = logloss(mid, y)
        parts = {"coin": "vec_coin", "ctx": "vec_ctx", "kal": "vec_kal"}
        W = {"Kalshi curve only": {"kal": 3.0}, "coin curve only": {"coin": 1.0}, "coin + context": {"coin": 1.0, "ctx": 0.5}, "everything": {"coin": 1.0, "ctx": 0.5, "kal": 3.0}}
        line = f"  minute {mnt:2}: Kalshi mid log-loss {base:.4f}; analog blends (with the mid, half and half in log-odds), gain over the mid per 1000 markets: "
        out = []
        for name, w in W.items():
            X = np.hstack([np.array([r[parts[k]] for r in tr]) * wt for k, wt in w.items()]).astype(np.float32)
            Q = np.hstack([np.array([r[parts[k]] for r in te]) * wt for k, wt in w.items()]).astype(np.float32)
            pk = np.zeros(len(te))
            d2 = (X ** 2).sum(1)
            for a in range(0, len(te), 600):
                q = Q[a:a + 600]
                d = (q ** 2).sum(1)[:, None] + d2[None, :] - 2 * q @ X.T
                idx = np.argpartition(d, K, axis=1)[:, :K]
                pk[a:a + 600] = ytr[idx].mean(axis=1)
            pa = (pk * K + 1) / (K + 2)
            blend = 1 / (1 + np.exp(-0.5 * (logit(mid) + logit(pa))))
            out.append(f"{name} {1000 * (base - logloss(blend, y)):+.2f}")
            if name == "everything":
                p_every = pa
        print(line + "; ".join(out) + f"  ({len(te)} test markets)")
        if mnt == 8:
            for bar in (0.03, 0.05):
                got = []
                for r, pp in zip(te, p_every, strict=True):
                    e_yes, e_no = pp - r["ask"], (1 - pp) - (1 - r["bid"])
                    if max(e_yes, e_no) >= bar:
                        price, win = (r["ask"], r["y"]) if e_yes >= e_no else (1 - r["bid"], 1 - r["y"])
                        got.append((((win - price - fee(price)) / (price + fee(price))), (win - price) / price))
                if len(got) >= 40:
                    g = np.array(got)
                    print(f"             buy rule on the everything memory, bar {bar:.2f}: {len(g)} trades, return with fee {g[:, 0].mean():+6.1%} +-{100 * g[:, 0].std() / math.sqrt(len(g)):.1f}, fee-free {g[:, 1].mean():+6.1%}")
