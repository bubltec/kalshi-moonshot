"""Pull Kalshi's 1-minute bid and ask candles for EVERY finished 15-minute crypto market in a period, with the batch endpoint (many markets per request). Read-only market data. Resumable.

    python scripts/kalshi_bulk_pull.py OUT_DIR START_ISO END_ISO
One JSON line per market: ticker, result, strike, open/close time and the candles (end time; bid and ask open/high/low/close; the last trade price; volume). 24 markets per request (the batch call caps markets x minutes of span).
"""
import datetime
import json
import os
import sys
import time

from kalshi_moonshot import config
from kalshi_moonshot.kalshi import KalshiClient

OUT, START, END = sys.argv[1], sys.argv[2], sys.argv[3]
s = config.reload()
c = KalshiClient(s.base_url, s.key_id, s.private_key_path, 2.0, s.page_size)
os.makedirs(OUT, exist_ok=True)
ts = lambda iso: int(datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())  # noqa: E731
lo, hi = ts(START), ts(END)
for series in ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M"):
    path = os.path.join(OUT, f"{series}.jsonl")
    have = {json.loads(line)["ticker"] for line in open(path)} if os.path.exists(path) else set()
    markets, cursor = [], None
    while True:
        params = {"series_ticker": series, "status": "settled", "min_close_ts": lo, "max_close_ts": hi, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        r = c._request("GET", "/markets", params=params)
        markets += r.get("markets", [])
        cursor = r.get("cursor")
        if not cursor or not r.get("markets"):
            break
    markets = [m for m in markets if m["ticker"] not in have and m.get("result") in ("yes", "no")]
    markets.sort(key=lambda m: m["close_time"])
    print(f"{series}: {len(markets)} to pull ({len(have)} already saved)", flush=True)
    with open(path, "a") as f:
        i = 0
        while i < len(markets):
            chunk = [markets[i]]
            while i + len(chunk) < len(markets):  # grow the chunk while markets x minutes of span stays under the batch cap (10,000 candles)
                cand = chunk + [markets[i + len(chunk)]]
                span = (max(ts(m["close_time"]) for m in cand) - min(ts(m["open_time"]) for m in cand)) / 60 + 2
                if span * len(cand) > 8500 or len(cand) > 60:
                    break
                chunk = cand
            i += len(chunk)
            st = min(ts(m["open_time"]) for m in chunk)
            en = max(ts(m["close_time"]) for m in chunk)
            for attempt in range(4):
                try:
                    got = c._request("GET", "/markets/candlesticks", params={"market_tickers": ",".join(m["ticker"] for m in chunk), "start_ts": st, "end_ts": en, "period_interval": 1}).get("markets", [])
                    break
                except Exception as exc:
                    print("  retry", str(exc)[:200], flush=True)
                    time.sleep(3 * (attempt + 1))
                    got = []
            if not got:
                print("  chunk failed; not saving it", flush=True)
                continue
            by = {g["market_ticker"]: g.get("candlesticks", []) for g in got}
            for m in chunk:
                rows = []
                for x in by.get(m["ticker"], []):
                    b, a = x["yes_bid"], x["yes_ask"]
                    rows.append({"t": x["end_period_ts"], "bo": b["open_dollars"], "bh": b["high_dollars"], "bl": b["low_dollars"], "bc": b["close_dollars"],
                                 "ao": a["open_dollars"], "ah": a["high_dollars"], "al": a["low_dollars"], "ac": a["close_dollars"], "px": (x.get("price") or {}).get("close_dollars"), "v": x.get("volume_fp")})
                f.write(json.dumps({"ticker": m["ticker"], "series": series, "open_ts": ts(m["open_time"]), "close_ts": ts(m["close_time"]), "result": m["result"], "strike": m.get("floor_strike"), "candles": rows}) + "\n")
            f.flush()
            print(f"  {series}: {i}/{len(markets)}", flush=True)
print("done", flush=True)
