"""Kalshi's authenticated WebSocket: live contract quotes and the settlement indexes themselves.

Two channels on one connection:

  ticker              every change of a market's bid / ask / last price, as it happens
  cfbenchmarks_value  the CF Benchmarks indexes Kalshi settles its crypto markets on (BRTI for bitcoin,
                      ETHUSD_RTI, ...), about once a second, each with its trailing 60-second average,
                      which is the quantity the 15-minute crypto markets compare

It is read-only: the connection is signed like any API call, but nothing here can place an order.
Events are handed to a callback as plain dicts; the callback must be quick.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from .kalshi import KalshiClient

log = logging.getLogger("moonshot")

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

# Kalshi series prefix -> the CF Benchmarks index that settles it.
INDEX_FOR = {
    "KXBTC": "BRTI", "KXETH": "ETHUSD_RTI", "KXSOL": "SOLUSD_RTI", "KXXRP": "XRPUSD_RTI",
    "KXDOGE": "DOGEUSD_RTI",
}


def index_for_series(series: str) -> str | None:
    for prefix, index in INDEX_FOR.items():
        if series.startswith(prefix) and series[len(prefix):] in {"", "D", "15M"}:
            return index
    return None


def _f(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_message(msg: dict[str, Any], received: float | None = None) -> dict[str, Any] | None:
    """A quote or an index tick as a flat dict; anything else (acks, errors, pings): None."""
    kind, body = msg.get("type"), msg.get("msg") or {}
    now = received if received is not None else time.time()
    if kind == "ticker":
        bid, ask = _f(body.get("yes_bid_dollars")), _f(body.get("yes_ask_dollars"))
        if not body.get("market_ticker") or bid is None or ask is None:
            return None
        return {"kind": "quote", "ts": now, "ticker": body["market_ticker"], "bid": bid, "ask": ask,
                "last": _f(body.get("price_dollars")), "sts": msg.get("sending_ts_ms"),
                "bid_size": _f(body.get("yes_bid_size_fp")), "ask_size": _f(body.get("yes_ask_size_fp"))}
    if kind == "trade":
        count, price = _f(body.get("count_fp")), _f(body.get("yes_price_dollars"))
        if not body.get("market_ticker") or not count or price is None:
            return None
        side = body.get("taker_outcome_side") or body.get("taker_side")  # the outcome the taker bought: "yes" or "no"
        if side not in {"yes", "no"}:
            return None
        return {"kind": "trade", "ts": now, "ticker": body["market_ticker"], "price": price, "count": count, "taker": side}
    if kind == "cfbenchmarks_value":
        try:
            frame = json.loads(body.get("data") or "{}")
        except ValueError:
            return None
        value = _f(frame.get("value"))
        if value is None or not body.get("index_id"):
            return None
        avg = _f((body.get("avg_60s_data") or {}).get("value"))
        win = _f((body.get("last_60s_windowed_average_15min") or {}).get("value"))
        return {"kind": "index", "ts": now, "index_id": body["index_id"], "value": value,
                "avg60": avg, "quarter_avg": win, "src_ms": frame.get("time"), "sts": msg.get("sending_ts_ms")}
    return None


class KalshiStream:
    """Keeps one connection open, reconnects with backoff, and resubscribes when the market set changes."""

    def __init__(self, client: KalshiClient, on_event: Callable[[dict[str, Any]], None],
                 indices: list[str] | None = None):
        self.client = client
        self.on_event = on_event
        self.indices = sorted(set(indices or INDEX_FOR.values()))
        self._tickers: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self._stop = False
        self.connected_at: float | None = None
        self.last_event_at: float | None = None

    def set_tickers(self, tickers: set[str]) -> None:
        """Replace the markets whose quotes are streamed. A change reconnects (cheap and robust)."""
        with self._lock:
            changed = tickers != self._tickers
            self._tickers = set(tickers)
        if changed and self._ws is not None and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._run, name="kalshi-ws", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop = True
        if self._ws is not None and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        import websockets

        self._loop = asyncio.get_running_loop()
        backoff = 1.0
        while not self._stop:
            try:
                async with websockets.connect(
                    WS_URL, additional_headers=self.client.ws_headers(WS_URL), open_timeout=15, ping_interval=20,
                ) as ws:
                    self._ws = ws
                    with self._lock:
                        tickers = sorted(self._tickers)
                    await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {
                        "channels": ["cfbenchmarks_value"], "index_ids": self.indices}}))
                    if tickers:
                        await ws.send(json.dumps({"id": 2, "cmd": "subscribe", "params": {
                            "channels": ["ticker", "trade"], "market_tickers": tickers}}))
                    self.connected_at, backoff = time.time(), 1.0
                    async for raw in ws:
                        self.handle(raw)
            except Exception as exc:
                log.warning("kalshi websocket: %s; reconnecting in %.0fs", exc, backoff)
            self._ws = None
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def handle(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if msg.get("type") == "error":
            log.warning("kalshi websocket error: %s", str(msg.get("msg"))[:160])
            return
        event = parse_message(msg)
        if event is not None:
            self.last_event_at = event["ts"]
            self.on_event(event)
