"""Pull 1-minute candles (time, open, close) from Coinbase's public candle endpoint for the five coins into one CSV each. Read-only; no account or key.

    python scripts/coinbase_minutes_pull.py OUT_DIR START_ISO [END_ISO]
Each request returns at most 300 candles, so this pages by 300 minutes; a minute with no trades has no candle (the reader forward-fills it).
"""
import csv
import datetime
import json
import os
import sys
import time
import urllib.request

OUT = sys.argv[1]
START = datetime.datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
END = datetime.datetime.fromisoformat(sys.argv[3].replace("Z", "+00:00")) if len(sys.argv) > 3 else datetime.datetime.now(datetime.UTC)
os.makedirs(OUT, exist_ok=True)
PRODUCTS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD", "DOGE": "DOGE-USD"}
for coin, prod in PRODUCTS.items():
    rows = {}
    t = START
    n = 0
    while t < END:
        t2 = min(t + datetime.timedelta(minutes=299), END)
        url = f"https://api.exchange.coinbase.com/products/{prod}/candles?granularity=60&start={t.isoformat()}&end={t2.isoformat()}"
        for attempt in range(5):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "kalshi-moonshot-research"})
                for c in json.load(urllib.request.urlopen(req, timeout=30)):
                    rows[c[0]] = (c[3], c[4])  # open, close
                break
            except Exception:
                time.sleep(1.5 * (attempt + 1))
        n += 1
        t = t2
        time.sleep(0.12)
    with open(os.path.join(OUT, f"{coin}.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "close"])
        for k in sorted(rows):
            w.writerow([k, *rows[k]])
    print(f"{coin}: {len(rows):,} minutes from {datetime.datetime.fromtimestamp(min(rows), datetime.UTC):%Y-%m-%d %H:%M} to {datetime.datetime.fromtimestamp(max(rows), datetime.UTC):%Y-%m-%d %H:%M} ({n} requests)", flush=True)
