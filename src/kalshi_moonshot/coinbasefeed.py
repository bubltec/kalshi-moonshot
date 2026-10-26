"""Real-time spot prices from Coinbase's public WebSocket, recorded on the tape next to the settlement index and the Kalshi quotes.

Coinbase is the largest US spot venue and one of the exchanges the settlement index is built from, so its trades are a live look at the same price from an exchange that is not averaged or smoothed. Binance, the biggest venue, is not reachable
from the US. A price per product is stored about once a second as `coinbase:BTC-USD` etc. in feed_ticks (no key, no account). The question it exists to answer: does Coinbase move before the index and before Kalshi's own price, by how many seconds,
and is that enough to warn of a last-minute drop? (leadlag-style studies on feed_ticks answer it once a few days are recorded.) Nothing here places an order.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

log = logging.getLogger("moonshot")

WS_URL = "wss://ws-feed.exchange.coinbase.com"
DEFAULT_PRODUCTS = ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD")


def parse_ticker(msg: dict[str, Any]) -> tuple[str, float, float] | None:
    """(product, unix time, price) of a live `ticker` message (one per trade). Anything else, or a message without a usable price: None."""
    if msg.get("type") != "ticker" or not msg.get("product_id") or not msg.get("price"):
        return None
    try:
        ts = datetime.fromisoformat(str(msg["time"])).timestamp()
        return str(msg["product_id"]), ts, float(msg["price"])
    except (KeyError, TypeError, ValueError):
        return None


class CoinbaseFeed:
    """Subscribes to the ticker of each product and hands every live trade price to `sink(product, seconds, price)`."""

    def __init__(self, products: Iterable[str], sink: Callable[[str, float, float], None]):
        self.products = tuple(products)
        self.sink = sink
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self._stop = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), name="coinbase-ws", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop = True
        if self._loop and self._ws:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)

    def handle(self, raw: str | bytes) -> None:
        try:
            got = parse_ticker(json.loads(raw))
        except ValueError:
            return
        if got is not None:
            try:
                self.sink(*got)
            except Exception:
                log.exception("coinbase feed: sink failed")

    async def _main(self) -> None:
        import websockets

        self._loop = asyncio.get_running_loop()
        backoff = 1.0
        while not self._stop:
            try:
                async with websockets.connect(WS_URL, open_timeout=15, ping_interval=20) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({"type": "subscribe", "product_ids": list(self.products), "channels": ["ticker"]}))
                    backoff = 1.0
                    async for raw in ws:
                        self.handle(raw)
            except Exception as exc:
                log.warning("coinbase websocket: %s; reconnecting in %.0fs", exc, backoff)
            self._ws = None
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
