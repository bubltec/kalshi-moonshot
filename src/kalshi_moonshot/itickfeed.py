"""iTick prices, from its websocket (https://docs.itick.io/). Records the S&P 500 index itself (symbol SPX, region GB) as tape index IT_SPX, at most one tick a second like the other indexes, next to BiQuote's US500 CFD (BQ_SPX), so the tape can say which one tracks the KXINX15M settlement better. 2026-11-10, user: add the S&P 15-minute markets; the free plan allows only 3 subscriptions in all, so this is the one symbol (commodities and Treasury yields would need a paid plan, or are not carried).
Needs an API key (ITICK_API_KEY, or ITICK_API_KEY_PATH naming a file that holds it); without one nothing starts. Record only: places nothing."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("itickfeed")

URL = "wss://api-free.itick.io/indices"
# tape index id -> iTick "symbol$region"
FEEDS: dict[str, str] = {"IT_SPX": "SPX$GB"}
_BY_SYMBOL = {symbol.split("$")[0]: index for index, symbol in FEEDS.items()}


def api_key() -> str:
    key = os.environ.get("ITICK_API_KEY", "").strip()
    path = os.environ.get("ITICK_API_KEY_PATH", "").strip()
    if not key and path:
        try:
            key = Path(path).read_text().strip()
        except OSError:
            log.warning("itick: cannot read ITICK_API_KEY_PATH %s", path)
    return key


def parse_message(frame: dict[str, Any], received: float) -> list[dict[str, Any]]:
    """The index events in one quote message: one for a symbol we record with a usable latest price."""
    data = frame.get("data")
    if frame.get("code") != 1 or not isinstance(data, dict) or data.get("type") != "quote":
        return []
    index = _BY_SYMBOL.get(str(data.get("s")))
    try:
        value = float(data["ld"])
    except (KeyError, TypeError, ValueError):
        return []
    if index is None or value <= 0 or data.get("ts") not in (0, None):  # ts 0 = normal trading
        return []
    src = data.get("t")
    return [{"kind": "index", "ts": received, "index_id": index, "value": value, "avg60": None, "quarter_avg": None,
             "src_ms": float(src) if isinstance(src, (int, float)) else None, "sts": None}]


class ITickFeed:
    def __init__(self, on_event: Callable[[dict[str, Any]], None], key: str):
        self.on_event = on_event
        self.key = key
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sent: dict[str, int] = {}
        self.last_event_at: float | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), name="itick", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()

    def handle(self, raw: str, now: float | None = None) -> None:
        """One frame; a tick is recorded at most once a second per index."""
        now = time.time() if now is None else now
        try:
            frame = json.loads(raw)
        except ValueError:
            return
        for event in parse_message(frame, now):
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
                async with websockets.connect(URL, additional_headers={"token": self.key}, open_timeout=15) as ws:
                    await ws.send(json.dumps({"ac": "subscribe", "params": ",".join(FEEDS.values()), "types": "quote"}))
                    backoff = 1.0
                    log.info("itick stream connected: %d symbols", len(FEEDS))
                    last_ping = time.time()
                    while not self._stop.is_set():
                        try:
                            self.handle(str(await asyncio.wait_for(ws.recv(), 20)))
                        except asyncio.TimeoutError:
                            pass
                        if time.time() - last_ping >= 20:  # the server drops a connection with no heartbeat for a minute
                            await ws.send(json.dumps({"ac": "ping", "params": str(int(time.time() * 1000))}))
                            last_ping = time.time()
            except Exception as exc:
                log.warning("itick stream: %s; reconnecting in %.0fs", type(exc).__name__, backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)
