"""Finnhub's free websocket, for the commodity markets: OANDA's gold, silver, platinum, palladium, WTI, Brent, natural gas and copper contracts. A free key (FINNHUB_API_KEY, or FINNHUB_API_KEY_PATH naming a file that holds it); without one nothing starts.

Each symbol becomes a tape index named in FEEDS, at most one tick a second like the crypto indexes. The levels differ from Pyth's (oil, gas and copper are other contract months; the strike is Pyth's), so a replay applies the move since the window opened to Kalshi's strike. Record only: nothing trades on it.
"""

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

log = logging.getLogger("finnhubfeed")

URL = "wss://ws.finnhub.io"

# tape index id -> Finnhub symbol
FEEDS: dict[str, str] = {
    "FH_GOLD": "OANDA:XAU_USD",
    "FH_SILVER": "OANDA:XAG_USD",
    "FH_PLATINUM": "OANDA:XPT_USD",
    "FH_PALLADIUM": "OANDA:XPD_USD",
    "FH_WTI": "OANDA:WTICO_USD",
    "FH_BRENT": "OANDA:BCO_USD",
    "FH_NATGAS": "OANDA:NATGAS_USD",
    "FH_COPPER": "OANDA:XCU_USD",
}
_BY_SYMBOL = {symbol: index for index, symbol in FEEDS.items()}


def api_key() -> str:
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    path = os.environ.get("FINNHUB_API_KEY_PATH", "").strip()
    if not key and path:
        try:
            key = Path(path).read_text().strip()
        except OSError:
            log.warning("finnhub: cannot read FINNHUB_API_KEY_PATH %s", path)
    return key


def parse_message(frame: dict[str, Any], received: float) -> list[dict[str, Any]]:
    """The index events in one trade message: one per usable trade of a symbol we record."""
    out: list[dict[str, Any]] = []
    if frame.get("type") != "trade":
        return out
    for t in frame.get("data") or []:
        index = _BY_SYMBOL.get(str(t.get("s")))
        try:
            value = float(t["p"])
        except (KeyError, TypeError, ValueError):
            continue
        if index is None or value <= 0:
            continue
        src = t.get("t")
        out.append({"kind": "index", "ts": received, "index_id": index, "value": value, "avg60": None, "quarter_avg": None,
                    "src_ms": float(src) if isinstance(src, (int, float)) else None, "sts": None})
    return out


class FinnhubFeed:
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
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), name="finnhub", daemon=True)
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
                async with websockets.connect(f"{URL}?token={self.key}", open_timeout=15) as ws:
                    for symbol in _BY_SYMBOL:
                        await ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))
                    backoff = 1.0
                    log.info("finnhub stream connected: %d symbols", len(_BY_SYMBOL))
                    while not self._stop.is_set():
                        self.handle(str(await asyncio.wait_for(ws.recv(), 60)))
            except Exception as exc:
                log.warning("finnhub stream: %s; reconnecting in %.0fs", type(exc).__name__, backoff)
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)
