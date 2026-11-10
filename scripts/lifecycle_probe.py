"""What the market_lifecycle_v2 channel sends for our 15-minute series around a window open (read-only). 2026-11-10, user: the strike and new markets arrive on the socket, "this avoids making api calls". Prints each event for a ticker containing "15M-" with how many seconds after the quarter-hour it arrived.
    scripts/cloud.sh run scripts/lifecycle_probe.py SECONDS=240
"""

import asyncio
import json
import os
import time

import websockets

from kalshi_moonshot import config
from kalshi_moonshot.__main__ import _client
from kalshi_moonshot.kalshiws import WS_URL

client = _client(config.reload())
SECS = float(os.environ.get("SECONDS", "240"))
counts = {}


async def run():
    async with websockets.connect(WS_URL, additional_headers=client.ws_headers(WS_URL), open_timeout=15, ping_interval=20) as ws:
        await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {"channels": ["market_lifecycle_v2"]}}))
        end = time.time() + SECS
        total = 0
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(end - time.time(), 0.1))
            except asyncio.TimeoutError:
                break
            total += 1
            d = json.loads(raw)
            m = d.get("msg") or {}
            tk = m.get("market_ticker", "")
            if "15M-" not in tk:
                continue
            key = (tk.split("-")[0], m.get("event_type"))
            counts[key] = counts.get(key, 0) + 1
            now = time.time()
            if counts[key] <= 2:
                slim = {k: v for k, v in m.items() if k not in ("additional_metadata", "price_ranges")}
                print(f"{time.strftime('%H:%M:%S', time.gmtime(now))} (+{now % 900:.1f}s after the quarter) {json.dumps(slim)[:420]}", flush=True)
        print("all lifecycle messages", total)


asyncio.run(run())
print("events by (series, type):")
by = {}
for (ser, ev), n in sorted(counts.items()):
    by.setdefault(ev, []).append(f"{ser} {n}")
for ev, v in by.items():
    print(" ", ev, "|", ", ".join(v)[:600])
