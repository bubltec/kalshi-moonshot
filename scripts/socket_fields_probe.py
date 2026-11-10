"""Print the raw Kalshi socket messages for one open market (read-only): which fields arrive, and is any live underlying value among them? (2026-11-10, user: "the spot value is in the socket")
    scripts/cloud.sh run scripts/socket_fields_probe.py
"""

import asyncio
import contextlib
import json
import time

import websockets

from kalshi_moonshot import config
from kalshi_moonshot.__main__ import _client
from kalshi_moonshot.kalshiws import WS_URL

s = config.reload()
client = _client(s)
ms = [m for m in client.series_markets("KXWTI15M") if m.get("status") in ("open", "active")]
ticker = ms[0]["ticker"] if ms else None
assert ticker, "no open KXWTI15M market"
print("market", ticker, flush=True)
keys = {}


async def run():
    async with websockets.connect(WS_URL, additional_headers=client.ws_headers(WS_URL), open_timeout=15, ping_interval=20) as ws:
        await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {"channels": ["ticker", "market_lifecycle_v2"], "market_tickers": [ticker]}}))
        end = time.time() + 25
        n = 0
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=end - time.time())
            except asyncio.TimeoutError:
                break
            d = json.loads(raw)
            keys.setdefault(d.get("type"), set()).update((d.get("msg") or {}).keys())
            if n < 8:
                print(raw[:600], flush=True)
            n += 1
        print("messages", n)


with contextlib.suppress(KeyboardInterrupt):
    asyncio.run(run())
for t, k in keys.items():
    print("type", t, "fields", sorted(k))
