"""BiQuote prices for the commodity markets, from its free SignalR hub (https://biquote.io/docs/). No key. Each symbol becomes a tape index named in FEEDS, at most one tick a second like the crypto indexes, so the tape can replay how well it calls the settled direction against Kalshi's strike (the move since the window opened, applied to the strike).

The metals come from a MetaTrader 5 broker feed, oil, natural gas and copper from BiQuote's aggregated perpetuals; the broker's oil symbols are recorded too, to compare. Record only: nothing trades on it. Off with BIQUOTE_FEED=0.
SignalR's JSON protocol is a websocket carrying messages that each end in the record separator: a handshake, then invocations (type 1), pings (type 6) and the server's calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx

log = logging.getLogger("biquotefeed")

HOST = "biquote.io"
RS = "\x1e"

# tape index id -> BiQuote symbol
FEEDS: dict[str, str] = {
    "BQ_GOLD": "XAUUSD",
    "BQ_SILVER": "XAGUSD",
    "BQ_PLATINUM": "XPTUSD",
    "BQ_PALLADIUM": "XPDUSD",
    "BQ_WTI": "CLUSDT",
    "BQ_BRENT": "BZUSDT",
    "BQ_NATGAS": "NATGASUSDT",
    "BQ_COPPER": "COPPERUSDT",
    "BQ_WTI_MT5": "USOIL",
    "BQ_BRENT_MT5": "UKOIL",
}
_BY_SYMBOL = {symbol: index for index, symbol in FEEDS.items()}


def enabled() -> bool:
    return os.environ.get("BIQUOTE_FEED", "1").strip().lower() not in {"0", "off", "false", "no"}


def parse_tick(tick: dict[str, Any], received: float) -> dict[str, Any] | None:
    """The index event in one ReceiveTick payload, or None for a symbol we do not record or a price that is not usable."""
    index = _BY_SYMBOL.get(str(tick.get("symbol")))
    try:
        value = float(tick["mid"])
    except (KeyError, TypeError, ValueError):
        return None
    if index is None or value <= 0:
        return None
    src_ms = None
    with contextlib.suppress(KeyError, ValueError):
        src_ms = datetime.fromisoformat(str(tick["timestamp"])).timestamp() * 1000.0
    return {"kind": "index", "ts": received, "index_id": index, "value": value, "avg60": None, "quarter_avg": None, "src_ms": src_ms, "sts": None}


class BiQuoteFeed:
    def __init__(self, on_event: Callable[[dict[str, Any]], None], host: str = HOST):
        self.on_event = on_event
        self.host = host
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sent: dict[str, int] = {}
        self.last_event_at: float | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), name="biquote", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()

    def handle(self, raw: str, now: float | None = None) -> None:
        """One websocket frame: any number of messages. A tick is recorded at most once a second per index."""
        now = time.time() if now is None else now
        for part in raw.split(RS):
            if not part:
                continue
            try:
                msg = json.loads(part)
            except ValueError:
                continue
            if msg.get("target") != "ReceiveTick" or not msg.get("arguments"):
                continue
            event = parse_tick(msg["arguments"][0], now)
            if event is None:
                continue
            second = int(event["ts"])
            if self._sent.get(event["index_id"]) == second:
                continue
            self._sent[event["index_id"]] = second
            self.last_event_at = now
            self.on_event(event)

    async def _main(self) -> None:
        import websockets

        backoff = 1.0
        while not self._stop.is_set():
            try:
                with httpx.Client(timeout=10.0) as http:
                    neg = http.post(f"https://{self.host}/hubs/tick/negotiate", params={"negotiateVersion": 1}).json()
                token = neg.get("connectionToken") or neg.get("connectionId")
                async with websockets.connect(f"wss://{self.host}/hubs/tick?id={token}", open_timeout=15) as ws:
                    await ws.send(json.dumps({"protocol": "json", "version": 1}) + RS)
                    await ws.recv()
                    await ws.send(json.dumps({"type": 1, "invocationId": "1", "target": "Subscribe", "arguments": [sorted(_BY_SYMBOL)]}) + RS)
                    backoff = 1.0
                    log.info("biquote stream connected: %d symbols", len(_BY_SYMBOL))
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), 30)  # ticks and pings arrive far more often than this
                        if '"type":6' in raw:
                            await ws.send(json.dumps({"type": 6}) + RS)
                        self.handle(str(raw))
            except Exception as exc:
                log.warning("biquote stream: %s; reconnecting in %.0fs", exc, backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)
