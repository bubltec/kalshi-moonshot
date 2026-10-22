"""Do the 15-minute markets' underlyings move with the S&P 500 / Nasdaq-100, and does the index's trend predict them?

Uses Hyperliquid 1-minute candles (xyz:SP500, xyz:XYZ100, and the other perps) over the last week. For each instrument:
  1. correlation of its 15-minute returns with the S&P's over the same 15 minutes (does it move together?)
  2. lead-lag on 1-minute returns: does the S&P's move 1-5 minutes ago line up with the instrument's move now?
  3. does the sign of the S&P's PREVIOUS 15 minutes predict the instrument's NEXT 15 minutes (the thing a bet would need)?
Public data only, no orders.    python scripts/market_context_study.py [days]
"""
import json
import math
import statistics
import sys
import time
import urllib.request

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 7
ASSETS = {"BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "XRP": "XRP", "DOGE": "DOGE", "GOLD": "xyz:GOLD", "SILVER": "xyz:SILVER",
          "WTI": "xyz:CL", "COPPER": "xyz:COPPER", "NATGAS": "xyz:NATGAS", "EUR": "xyz:EUR", "GBP": "xyz:GBP", "JPY": "xyz:JPY",
          "DXY": "xyz:DXY", "NASDAQ100": "xyz:XYZ100"}
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


def corr(a, b):
    if len(a) < 30:
        return float("nan")
    ma, mb = statistics.mean(a), statistics.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return num / den if den else float("nan")


def returns(series, step_s, lo, hi):
    """Log returns over consecutive `step_s` blocks aligned to the clock."""
    out = {}
    t = (lo // step_s + 1) * step_s
    while t + step_s <= hi:
        a, b = series.get(t), series.get(t + step_s)
        if a and b:
            out[t] = math.log(b / a)
        t += step_s
    return out


sp = candles(INDEX)
print(f"{len(sp)} one-minute S&P candles ({DAYS} days)\n")
print(f"{'':10} {'15m corr':>9} {'1m corr':>8} {'lag1':>6} {'lag2':>6} {'lag3':>6} {'lag5':>6}   prev-15m S&P sign -> next 15m: agree  n   (bigger S&P moves: agree  n)")
for name, coin in ASSETS.items():
    s = candles(coin)
    if not s:
        print(f"{name:10} no 1-minute candles available")
        continue
    lo, hi = max(min(sp), min(s)), min(max(sp), max(s))
    spr15, ar15 = returns(sp, 900, lo, hi), returns(s, 900, lo, hi)
    common = sorted(set(spr15) & set(ar15))
    c15 = corr([spr15[t] for t in common], [ar15[t] for t in common])
    spr1, ar1 = returns(sp, 60, lo, hi), returns(s, 60, lo, hi)
    cm = sorted(set(spr1) & set(ar1))
    c1 = corr([spr1[t] for t in cm], [ar1[t] for t in cm])
    lags = []
    for k in (1, 2, 3, 5):  # the S&P's move k minutes ago against the instrument's move now
        pairs = [(spr1[t - 60 * k], ar1[t]) for t in ar1 if (t - 60 * k) in spr1]
        lags.append(corr([p[0] for p in pairs], [p[1] for p in pairs]))
    hit = tot = bhit = btot = 0
    sd = statistics.pstdev(spr15.values()) if spr15 else 0
    for t in common:
        prev = spr15.get(t - 900)
        if prev is None or ar15[t] == 0 or prev == 0:
            continue
        tot += 1
        hit += (prev > 0) == (ar15[t] > 0)
        if abs(prev) > sd:
            btot += 1
            bhit += (prev > 0) == (ar15[t] > 0)
    print(f"{name:10} {c15:>9.2f} {c1:>8.2f} {lags[0]:>6.2f} {lags[1]:>6.2f} {lags[2]:>6.2f} {lags[3]:>6.2f}   "
          f"{hit / tot if tot else float('nan'):>34.1%} {tot:>4}   ({bhit / btot if btot else float('nan'):>5.1%} {btot:>4})")
