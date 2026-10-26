"""Pull Kalshi's own 1-minute bid and ask candles for a sample of finished 15-minute crypto markets. Read-only (public market data through the bot's client, no orders). Resumable.

    python scripts/kalshi_candle_pull.py OUT_DIR START_ISO END_ISO PER_COIN
Lists the settled markets of each coin's series between START and END (the live endpoint reaches back to about 2026-08-26), takes PER_COIN of them spread evenly over the period, and writes one JSON line per market
with its result, strike and the 1-minute candles (end time, bid and ask open/close). One request per market, throttled to 1 per second so the running bot keeps its rate budget.
"""
import datetime
import json
import os
import sys
import time

from kalshi_moonshot import config
from kalshi_moonshot.kalshi import KalshiClient

OUT, START, END, PER_COIN = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
s = config.reload()
c = KalshiClient(s.base_url, s.key_id, s.private_key_path, 1.0, s.page_size)
os.makedirs(OUT, exist_ok=True)
ts = lambda iso: int(datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())  # noqa: E731
lo, hi = ts(START), ts(END)
for series in ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M"):
    path = os.path.join(OUT, f"{series}.jsonl")
    have = set()
    if os.path.exists(path):
        have = {json.loads(line)["ticker"] for line in open(path)}
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
    markets.sort(key=lambda m: m["close_time"])
    step = max(1, len(markets) // PER_COIN)
    chosen = markets[::step][:PER_COIN]
    print(f"{series}: {len(markets)} settled markets listed, taking {len(chosen)}; {len(have)} already saved", flush=True)
    with open(path, "a") as f:
        for i, m in enumerate(chosen):
            if m["ticker"] in have or m.get("result") not in ("yes", "no"):
                continue
            o, cl = ts(m["open_time"]), ts(m["close_time"])
            try:
                cs = c._request("GET", f"/series/{series}/markets/{m['ticker']}/candlesticks", params={"start_ts": o, "end_ts": cl, "period_interval": 1}).get("candlesticks", [])
            except Exception as exc:
                print("  error", m["ticker"], str(exc)[:80], flush=True)
                continue
            rows = [{"t": x["end_period_ts"], "bid_o": x["yes_bid"]["open_dollars"], "bid_c": x["yes_bid"]["close_dollars"], "ask_o": x["yes_ask"]["open_dollars"], "ask_c": x["yes_ask"]["close_dollars"],
                     "px": (x.get("price") or {}).get("close_dollars")} for x in cs]
            f.write(json.dumps({"ticker": m["ticker"], "series": series, "open_ts": o, "close_ts": cl, "result": m["result"], "strike": m.get("floor_strike"), "candles": rows}) + "\n")
            f.flush()
            if (i + 1) % 50 == 0:
                print(f"  {series}: {i + 1}/{len(chosen)}", flush=True)
print("done", flush=True)
