"""Does net taker buying in the first minutes of a quarter-hour say which side of its opening price BTC or ETH finishes on, beyond what the price has already done?

Uses 1-minute Binance spot candles (the KEDevO/crypto-market-datasets files; each has the taker-buy share of volume). A window is a UTC quarter-hour,
`up` means its last close is above its first open (what a 15-minute Kalshi market asks, up to the index difference). At decision minute m it sees only
minutes 0..m-1. The baseline model knows only how far the price has gone in units of what it could still move (the same quantity the bot's pricer uses);
the second model adds net taker imbalance and a volume surge. Fit on 2022-2024, scored on 2025 onward, so an improvement has to hold on data it never saw.

    python scripts/flow_15m_study.py SPOT_DIR [FUTURES_DIR]    (each holds BTCUSDT_*.parquet and ETHUSDT_*.parquet; needs numpy and pyarrow)

With a futures folder it also asks whether the perpetual futures' taker flow, and how far their price has moved ahead of spot, add to the price so far.

Read-only analysis of downloaded files. Places nothing.
"""
import glob
import math
import sys

import numpy as np
import pyarrow.parquet as pq

DIR = sys.argv[1] if len(sys.argv) > 1 else "."
FDIR = sys.argv[2] if len(sys.argv) > 2 else None
MINUTES = (3, 5, 8, 10)
SPLIT_MS = 1735689600000  # 2025-01-01 UTC
COST = 0.03  # a rough round-trip cost per contract in dollars: fee about 1.7c plus a cent of spread, rounded up


def load(symbol, folder=None):
    folder = folder or DIR
    cols = ["open_time", "open", "close", "volume", "taker_buy_base", "trades"]
    parts = [pq.read_table(f, columns=cols) for f in sorted(glob.glob(f"{folder}/{symbol}_*.parquet"))]
    t = {c: np.concatenate([p.column(c).to_numpy() for p in parts]) for c in cols}
    order = np.argsort(t["open_time"], kind="stable")
    t = {c: v[order] for c, v in t.items()}
    keep = np.concatenate([[True], np.diff(t["open_time"]) > 0])  # the monthly files overlap a little
    t = {c: v[keep].astype(float) for c, v in t.items()}
    mid = (t["open_time"] // 60000).astype(np.int64)
    lo = (mid.min() // 15) * 15
    n = ((mid.max() - lo) // 15 + 1) * 15
    grid = {}
    for c in cols[1:]:
        g = np.full(n, np.nan)
        g[mid - lo] = t[c]
        grid[c] = g.reshape(-1, 15)
    start_ms = (lo + 15 * np.arange(n // 15)) * 60000
    return grid, start_ms


def logistic(X, y, iters=40):
    X1 = np.column_stack([np.ones(len(X)), X])
    b = np.zeros(X1.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(X1 @ b, -30, 30)))
        W = p * (1 - p) + 1e-9
        H = X1.T @ (X1 * W[:, None]) + 1e-9 * np.eye(len(b))
        step = np.linalg.solve(H, X1.T @ (y - p))
        b += step
        if np.abs(step).max() < 1e-8:
            break
    se = np.sqrt(np.diag(np.linalg.inv(H)))
    return b, se


def predict(b, X):
    return 1 / (1 + np.exp(-np.clip(np.column_stack([np.ones(len(X)), X]) @ b, -30, 30)))


def logloss(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def features(g, start_ms, m):
    o, c, v, tb = g["open"], g["close"], g["volume"], g["taker_buy_base"]
    ok = ~np.isnan(g["close"]).any(axis=1) & ~np.isnan(v).any(axis=1) & (v[:, :m].sum(axis=1) > 0)
    first = o[:, 0]
    ret = c[:, m - 1] / first - 1.0
    lr = np.log(c / o)  # each minute's own log return
    flat = lr.reshape(-1)
    sig = np.full(len(first), np.nan)  # trailing volatility per minute: the 120 minutes before the window
    csum = np.nancumsum(np.nan_to_num(flat) ** 2)
    idx = np.arange(len(first)) * 15
    for i in range(8, len(first)):
        a = idx[i] - 120
        if a >= 0:
            sig[i] = math.sqrt((csum[idx[i] - 1] - csum[a - 1 if a > 0 else 0]) / 120)
    left = 15 - m
    dist = ret / (sig * math.sqrt(left))  # sigmas still to move: how far from "level" the price already is, at the horizon that remains
    net = 2 * tb - v
    imb = net[:, :m].sum(axis=1) / v[:, :m].sum(axis=1)
    imb2 = net[:, m - 2:m].sum(axis=1) / np.maximum(v[:, m - 2:m].sum(axis=1), 1e-12)
    vpm = v[:, :m].sum(axis=1) / m
    base = np.full(len(first), np.nan)
    for i in range(8, len(first)):
        a = idx[i] - 120
        if a >= 0:
            base[i] = np.nanmean(g["volume"].reshape(-1)[a:idx[i]])
    surge = np.log(vpm / base)
    y = (c[:, 14] > first).astype(float)
    good = ok & np.isfinite(dist) & np.isfinite(surge) & np.isfinite(imb) & np.isfinite(imb2) & (np.abs(dist) < 12)
    return dist[good], imb[good], imb2[good], surge[good], y[good], start_ms[good]


_FUT = {}


def futures_block(g, start, symbol, m, d, imb, imb2, surge, y, st, tr, te, llA, bA, A):
    if symbol not in _FUT:
        _FUT[symbol] = load(symbol, FDIR)
    gf, sf = _FUT[symbol]
    dF, imbF, imb2F, surgeF, yF, stF = features(gf, sf, m)
    common, i_s, i_f = np.intersect1d(st, stF, return_indices=True)
    d, imb, imb2, surge, y, st = d[i_s], imb[i_s], imb2[i_s], surge[i_s], y[i_s], st[i_s]
    dF, imbF, imb2F, surgeF = dF[i_f], imbF[i_f], imb2F[i_f], surgeF[i_f]
    tr, te = st < SPLIT_MS, st >= SPLIT_MS
    z = lambda x: (x - x[tr].mean()) / x[tr].std()
    lead = dF - d  # how much further the futures price has already moved than spot, in sigmas
    base = np.column_stack([d])
    sets = {
        "futures flow + lead": np.column_stack([d, z(imbF), z(imb2F), z(surgeF), z(lead)]),
        "spot flow + futures flow + lead": np.column_stack([d, z(imb), z(imb2), z(surge), z(imbF), z(imb2F), z(surgeF), z(lead)]),
    }
    bA2, _ = logistic(base[tr], y[tr])
    pA = predict(bA2, base[te])
    ll0 = logloss(pA, y[te])
    print(f"   FUTURES ({int(len(y)):,} windows with both): baseline test log-loss {ll0:.5f}")
    for name, X in sets.items():
        b, se = logistic(X[tr], y[tr])
        pB = predict(b, X[te])
        ll = logloss(pB, y[te])
        names = ["distance", "spot imb", "spot imb2", "spot surge", "fut imb", "fut imb2", "fut surge", "lead"] if X.shape[1] == 8 else ["distance", "fut imb", "fut imb2", "fut surge", "lead"]
        coef = "  ".join(f"{n} {b[i + 1]:+.3f}(t={b[i + 1] / se[i + 1]:+.0f})" for i, n in enumerate(names) if n != "distance")
        edges = []
        for thr in (0.02, 0.04):
            up, dn = (pB - pA) > thr, (pA - pB) > thr
            e = np.concatenate([(y[te] - pA)[up], (pA - y[te])[dn]])
            edges.append(f">{thr:.0%}: {len(e):,} bets {e.mean() * 100:+.2f}c (±{e.std(ddof=1) / math.sqrt(len(e)) * 100:.2f})" if len(e) >= 30 else f">{thr:.0%}: {len(e)} bets")
        print(f"   {name:34} test log-loss {ll:.5f} ({'+' if ll < ll0 else '-'}{abs(ll0 - ll):.5f} vs baseline) | {coef}\n   {'':34} gross edge vs baseline price, cost to beat ~3c: " + " | ".join(edges))


def report(symbol):
    g, start = load(symbol)
    print(f"\n==================== {symbol}: {int(np.isfinite(g['close']).all(axis=1).sum()):,} complete quarter-hours ====================")
    for m in MINUTES:
        d, imb, imb2, surge, y, st = features(g, start, m)
        tr, te = st < SPLIT_MS, st >= SPLIT_MS
        z = lambda x: (x - x[tr].mean()) / x[tr].std()
        A = np.column_stack([d])
        B = np.column_stack([d, z(imb), z(imb2), z(surge)])
        bA, _ = logistic(A[tr], y[tr])
        bB, seB = logistic(B[tr], y[tr])
        pA, pB = predict(bA, A[te]), predict(bB, B[te])
        llA, llB = logloss(pA, y[te]), logloss(pB, y[te])
        print(f"\n-- decision at minute {m} of 15 (sees {m} minutes): train {int(tr.sum()):,} windows, test {int(te.sum()):,}; up-rate {y.mean():.3f}")
        print(f"   baseline (price so far only): test log-loss {llA:.5f}   with flow added: {llB:.5f}   ({'better' if llB < llA else 'worse'} by {abs(llA - llB):.5f})")
        names = ["distance", "imbalance", "last-2-min imbalance", "volume surge"]
        print("   coefficients on the training years (logit per 1 s.d.):  " + "  ".join(f"{n} {bB[i + 1]:+.3f} (t={bB[i + 1] / seB[i + 1]:+.1f})" for i, n in enumerate(names)))
        # would it have paid? treat the baseline as the market price; bet where the flow model disagrees by at least 3 cents
        for thr in (0.02, 0.04):
            up, dn = (pB - pA) > thr, (pA - pB) > thr
            edge = np.concatenate([(y[te] - pA)[up], (pA - y[te])[dn]])
            if len(edge) >= 30:
                se = edge.std(ddof=1) / math.sqrt(len(edge))
                print(f"   bet when the flow model differs from the baseline by >{thr:.0%}: {len(edge):>6,} bets, gross edge {edge.mean() * 100:+.2f} cents per $1 contract (±{se * 100:.2f}); cost to beat ~{COST * 100:.0f} cents")
            else:
                print(f"   bet when the flow model differs by >{thr:.0%}: only {len(edge)} bets")
        if FDIR:
            futures_block(g, start, symbol, m, d, imb, imb2, surge, y, st, tr, te, logloss(pA, y[te]), bA, A)
        # the plain picture: up-rate by imbalance fifth, within the middle of the price-so-far range
        mid = tr | te
        sel = np.abs(d) < 0.6
        q = np.quantile(imb[sel], [0, .2, .4, .6, .8, 1])
        rates = [y[sel & (imb >= q[i]) & (imb <= q[i + 1])].mean() for i in range(5)]
        print("   when the price so far is near level (|distance| < 0.6), up-rate by net-buying fifth (low to high): " + "  ".join(f"{r:.3f}" for r in rates))


for sym in ("BTCUSDT", "ETHUSDT"):
    report(sym)
