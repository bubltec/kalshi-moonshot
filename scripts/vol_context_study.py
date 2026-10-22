"""Does the S&P's recent volatility help predict an instrument's NEXT 15-minute volatility, beyond its own?

Realized variance over each 15-minute block from Hyperliquid 1-minute candles. For every instrument, in log space:
  own only:     log RV(next 15m) ~ log RV(own, previous 60m)
  with S&P:     log RV(next 15m) ~ log RV(own, previous 60m) + log RV(S&P, previous 60m)
Fit on the earliest 60% of blocks, scored on the latest 40% (out-of-sample R squared). Public data only; places no orders.
    python scripts/vol_context_study.py [days]
"""
import json
import math
import statistics
import sys
import time
import urllib.request

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 7
ASSETS = {"BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "XRP": "XRP", "DOGE": "DOGE", "GOLD": "xyz:GOLD", "SILVER": "xyz:SILVER",
          "WTI": "xyz:CL", "COPPER": "xyz:COPPER", "NATGAS": "xyz:NATGAS", "EUR": "xyz:EUR", "NASDAQ100": "xyz:XYZ100"}
INDEX = "xyz:SP500"


def candles(coin):
    out, end = {}, int(time.time() * 1000)
    start_all = end - DAYS * 86_400_000
    while end > start_all:
        start = max(start_all, end - 4900 * 60_000)
        body = {"type": "candleSnapshot", "req": {"coin": coin, "interval": "1m", "startTime": start, "endTime": end}}
        req = urllib.request.Request("https://api.hyperliquid.xyz/info", json.dumps(body).encode(), {"Content-Type": "application/json"})
        rows = json.load(urllib.request.urlopen(req, timeout=30))
        if not rows:
            break
        for r in rows:
            out[int(r["t"] // 1000)] = float(r["c"])
        end = start - 1
    return out


def rv(series, start, minutes):
    closes = [series.get(start + 60 * k) for k in range(minutes + 1)]
    if any(c is None for c in closes):
        return None
    return sum(math.log(b / a) ** 2 for a, b in zip(closes, closes[1:]))


def ols(X, y):
    """Least squares by the normal equations (small, dense)."""
    k = len(X[0])
    A = [[sum(r[i] * r[j] for r in X) for j in range(k)] for i in range(k)]
    b = [sum(r[i] * t for r, t in zip(X, y)) for i in range(k)]
    for i in range(k):
        piv = A[i][i] or 1e-12
        for j in range(i + 1, k):
            f = A[j][i] / piv
            A[j] = [a - f * c for a, c in zip(A[j], A[i])]
            b[j] -= f * b[i]
    w = [0.0] * k
    for i in reversed(range(k)):
        w[i] = (b[i] - sum(A[i][j] * w[j] for j in range(i + 1, k))) / (A[i][i] or 1e-12)
    return w


def r2(w, X, y):
    pred = [sum(a * b for a, b in zip(w, r)) for r in X]
    ym = statistics.mean(y)
    ss_res = sum((t - p) ** 2 for t, p in zip(y, pred))
    ss_tot = sum((t - ym) ** 2 for t in y)
    return 1 - ss_res / ss_tot


sp = candles(INDEX)
print(f"{len(sp)} one-minute S&P candles ({DAYS} days)\n")
print(f"{'':10} {'blocks':>6} {'R2 own only':>12} {'R2 + S&P':>9} {'gain':>7} {'S&P weight':>11}")
for name, coin in ASSETS.items():
    s = candles(coin)
    if not s:
        continue
    lo, hi = max(min(sp), min(s)), min(max(sp), max(s))
    rows = []
    t = (lo // 900 + 5) * 900
    while t + 900 <= hi:
        nxt = rv(s, t, 15)
        own = rv(s, t - 3600, 60)
        spv = rv(sp, t - 3600, 60)
        if nxt and own and spv:
            rows.append((t, math.log(nxt), math.log(own), math.log(spv)))
        t += 900
    if len(rows) < 80:
        continue
    cut = int(len(rows) * 0.6)
    tr, te = rows[:cut], rows[cut:]
    Xo = [[1.0, r[2]] for r in tr]
    Xs = [[1.0, r[2], r[3]] for r in tr]
    wo, ws = ols(Xo, [r[1] for r in tr]), ols(Xs, [r[1] for r in tr])
    ro = r2(wo, [[1.0, r[2]] for r in te], [r[1] for r in te])
    rs = r2(ws, [[1.0, r[2], r[3]] for r in te], [r[1] for r in te])
    print(f"{name:10} {len(rows):>6} {ro:>12.3f} {rs:>9.3f} {rs - ro:>+7.3f} {ws[2]:>+11.2f}")
