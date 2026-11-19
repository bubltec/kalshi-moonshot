"""Settled results of Kalshi's Crypto Leader 15 minute races (series KXCRYPTOLEAD15M), read from the public API (no account, read-only).
One line per market: ticker, close_ts (UTC epoch), result (yes/no). Run on the Mac:
  .venv/bin/python scripts/crypto_lead_fetch_results.py > studies/results/2026-11-19-crypto-lead-results.csv"""
import calendar
import time
from datetime import datetime

import httpx

B = "https://api.elections.kalshi.com/trade-api/v2/markets"
cur, n = None, 0
print("ticker,close_ts,result")
while True:
    p = {"series_ticker": "KXCRYPTOLEAD15M", "status": "settled", "limit": 1000}
    if cur:
        p["cursor"] = cur
    for a in range(10):
        r = httpx.get(B, params=p, timeout=60)
        if r.status_code != 429:
            break
        time.sleep(5 * (a + 1))
    j = r.json()
    for m in j.get("markets", []):
        ct = calendar.timegm(datetime.strptime(m["close_time"][:19], "%Y-%m-%dT%H:%M:%S").timetuple())
        print(f'{m["ticker"]},{ct},{m.get("result","")}')
        n += 1
    cur = j.get("cursor")
    if not cur or n > 20000:
        break
