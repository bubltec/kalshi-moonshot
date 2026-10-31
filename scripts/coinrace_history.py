"""Pull the coin race's settled races with their 1-minute bid/ask candles from Kalshi's public API and save them as JSON lines. Read-only; no account needed.

    docker compose ... exec -T -e RACES=400 -e OUT=/data/coinrace_history.jsonl bot python - < scripts/coinrace_history.py
One line per race: {"event", "close", "winner", "markets": {coin: {"result", "volume", "candles": [[end_ts, bid_close, ask_close, bid_low, ask_high], ...]}}}
"""
import collections
import json
import os
import time
from datetime import datetime

import httpx

B = "https://api.elections.kalshi.com/trade-api/v2"


def get(url, **kw):
    """GET with a wait and retry on a 429: the public API limits how fast a script may read."""
    for attempt in range(12):
        r = httpx.get(url, timeout=60, **kw)
        if r.status_code != 429:
            return r
        time.sleep(min(60.0, 5.0 * (attempt + 1)))
    raise RuntimeError("still rate limited after 12 tries")


RACES, OUT = int(os.environ.get("RACES", "400")), os.environ.get("OUT", "/data/coinrace_history.jsonl")
ms, cur = [], None
while len(ms) < RACES * 5 + 200:
    p = {"series_ticker": "KXCRYPTOLEAD15M", "status": "settled", "limit": 1000}
    if cur:
        p["cursor"] = cur
    r = get(B + "/markets", params=p).json()
    ms += r.get("markets", [])
    cur = r.get("cursor")
    if not cur:
        break
ev = collections.defaultdict(dict)
for m in ms:
    ev[m["event_ticker"]][m["yes_sub_title"]] = m
events = sorted((e for e, d in ev.items() if len(d) == 5), key=lambda e: ev[e]["BTC"]["close_time"], reverse=True)[:RACES]
ts = lambda x: int(datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp())  # noqa: E731
with open(OUT, "w") as f:
    for n, e in enumerate(events):
        d = ev[e]
        row = {"event": e, "close": ts(d["BTC"]["close_time"]), "winner": next((c for c, m in d.items() if m["result"] == "yes"), None), "markets": {}}
        for c, m in d.items():
            r = get(f"{B}/series/KXCRYPTOLEAD15M/markets/{m['ticker']}/candlesticks", params={"start_ts": ts(m["open_time"]), "end_ts": ts(m["close_time"]), "period_interval": 1})
            cs = r.json().get("candlesticks", []) if r.status_code == 200 else []
            g = lambda x, k: float((x.get(k) or 0) or 0)  # noqa: E731
            row["markets"][c] = {"result": m["result"], "volume": float(m.get("volume_fp") or 0), "candles": [[x["end_period_ts"], g(x["yes_bid"], "close_dollars"), g(x["yes_ask"], "close_dollars"), g(x["yes_bid"], "low_dollars"), g(x["yes_ask"], "high_dollars")] for x in cs]}
            time.sleep(0.3)
        f.write(json.dumps(row) + "\n")
        if n % 25 == 0:
            print(n, e, flush=True)
print("DONE")
