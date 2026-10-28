"""Polymarket US market socket: best bid and ask as they change.

The stop was reading the book over HTTP every 2 seconds, so a favorite could fall through 60c
between reads and the sale filled at the next print. This is the markets socket
(`wss://api.polymarket.us/v1/ws/markets`), signed the same way as a GET. It is read-only.
Events are Kalshi-shaped quotes, keyed by the ticker the lanes already use.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("moonshot")

WS_URL = "wss://api.polymarket.us/v1/ws/markets"
WS_PATH = "/v1/ws/markets"


def _amount(node: Any) -> float | None:
    if isinstance(node, dict):
        node = node.get("value")
    try:
        return float(node) if node not in (None, "") else None
    except (TypeError, ValueError):
        return None


def quote_from(msg: dict[str, Any], ticker_of: dict[str, str], now: float | None = None) -> dict[str, Any] | None:
    """A lite (or full-book) update as a quote the watcher already understands, or None."""
    lite = msg.get("marketDataLite") or msg.get("market_data_lite") or {}
    slug = lite.get("marketSlug") or lite.get("market_slug") or ""
    bid = _amount(lite.get("bestBid") if "bestBid" in lite else lite.get("best_bid"))
    ask = _amount(lite.get("bestAsk") if "bestAsk" in lite else lite.get("best_ask"))
    if not slug:
        book = msg.get("marketData") or msg.get("market_data") or {}
        slug = book.get("marketSlug") or book.get("market_slug") or ""
        bids, offers = book.get("bids") or [], book.get("offers") or []
        bid = _amount((bids[0] if bids else {}).get("px"))
        ask = _amount((offers[0] if offers else {}).get("px"))
    ticker = ticker_of.get(slug, "")
    if not ticker or bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    return {"kind": "quote", "ts": now if now is not None else time.time(), "ticker": ticker, "bid": bid, "ask": ask, "src": "poly"}


class PolymarketStream:
    """One markets connection. A change in the open bets reconnects and resubscribes."""

    def __init__(self, client: Any, on_event: Callable[[dict[str, Any]], None]):
        self.client = client
        self.on_event = on_event
        self._slugs: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self._stop = False
        self.connected_at: float | None = None
        self.last_event_at: float | None = None
        self._pong_at = 0.0

    def set_tickers(self, tickers: set[str]) -> None:
        """Follow these Kalshi-shaped tickers. A slug we have not listed yet is looked up once."""
        slugs: set[str] = set()
        for ticker in tickers:
            slug = self.client._slug_of.get(ticker, "")
            if not slug:
                try:
                    slug = self.client.market(ticker).get("polymarket_slug") or ""
                except Exception:
                    log.debug("polymarket socket: no slug for %s", ticker, exc_info=True)
                    continue
            if slug:
                slugs.add(slug)
        with self._lock:
            changed = slugs != self._slugs
            self._slugs = slugs
        if changed and self._ws is not None and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)

    def start(self) -> None:
        if not getattr(self.client, "authed", False):
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._run, name="polymarket-ws", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop = True
        if self._ws is not None and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)

    @property
    def connected(self) -> bool:
        return self._ws is not None

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        import websockets

        self._loop = asyncio.get_running_loop()
        backoff = 1.0
        while not self._stop:
            try:
                headers = self.client._headers("GET", WS_PATH)
                async with websockets.connect(WS_URL, additional_headers=headers, open_timeout=15, ping_interval=20) as ws:
                    self._ws = ws
                    with self._lock:
                        slugs = sorted(self._slugs)
                    if slugs:
                        await ws.send(json.dumps({
                            "subscribe": {
                                "requestId": "moonshot-bbo",
                                "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA_LITE",
                                "marketSlugs": slugs,
                            },
                        }))
                    self.connected_at, backoff = time.time(), 1.0
                    log.info("polymarket socket on: %d market(s)", len(slugs))
                    async for raw in ws:
                        await self._take(ws, raw)
            except Exception as exc:
                log.warning("polymarket websocket: %s; reconnecting in %.0fs", exc, backoff)
            self._ws = None
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _take(self, ws: Any, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if "heartbeat" in msg:
            if time.time() - self._pong_at > 5.0:
                self._pong_at = time.time()
                await ws.send(json.dumps({"heartbeat": {}}))
            return
        if msg.get("error"):
            log.warning("polymarket socket: %s", str(msg.get("error"))[:160])
            return
        event = quote_from(msg, self.client._ticker_of)
        if event is None:
            return
        self.last_event_at = event["ts"]
        try:
            self.on_event(event)
        except Exception:
            log.debug("polymarket socket: quote handler failed", exc_info=True)
