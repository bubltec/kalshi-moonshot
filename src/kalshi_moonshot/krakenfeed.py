"""Real-time prices from Kraken's public WebSocket v2 ticker (the same stream kraken-portfolio uses).

Kraken lists the spot pairs we need for crypto (BTC/ETH/SOL/XRP/DOGE) and two tokenized-gold pairs
(PAXG/USD, XAUT/USD) that track gold spot. A background thread keeps one connection open, reconnects
with backoff, and stores the latest trade price and its timestamp per symbol. History for realized
volatility comes from Kraken's public OHLC endpoint (5-minute bars), cached for a minute.

Symbols are written "kraken:XBT/USD" in instruments.toml. No API key is involved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import httpx

from .commodities import CommodityError, Feed, Series

log = logging.getLogger("moonshot")

WS_URL = "wss://ws.kraken.com/v2"
OHLC_URL = "https://api.kraken.com/0/public/OHLC"
PREFIX = "kraken:"
FIRST_TICK_WAIT = 8.0  # seconds to wait for the first price after subscribing
BARS_TTL = 60.0


def parse_ticker(msg: dict[str, Any]) -> list[tuple[str, float, float]]:
    """(symbol, last price, unix time) for each row of a v2 ticker message. Other messages: []."""
    if msg.get("channel") != "ticker" or msg.get("type") not in {"snapshot", "update"}:
        return []
    out: list[tuple[str, float, float]] = []
    for row in msg.get("data") or []:
        try:
            ts = datetime.fromisoformat(row["timestamp"]).timestamp()
            out.append((row["symbol"], float(row["last"]), ts))
        except (KeyError, TypeError, ValueError):
            continue
    return out


class KrakenFeed:
    def __init__(self, symbols: Iterable[str] = (), http: httpx.Client | None = None):
        self._symbols: set[str] = {s.removeprefix(PREFIX) for s in symbols}
        self._ticks: dict[str, tuple[float, float]] = {}
        self._bars: dict[str, tuple[float, list[float], list[float]]] = {}
        self._minutes: dict[str, tuple[float, dict[int, float]]] = {}
        self._lock = threading.Lock()
        self._arrived = threading.Condition(self._lock)
        self._http = http or httpx.Client(timeout=10.0)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self._stop = False

    def minute_closes(self, symbol: str, hours: float = 4.0) -> dict[int, float]:
        return _kraken_minutes(self, symbol, hours)

    def latest(self, symbol: str) -> tuple[float, float] | None:
        """(price, Kraken's own timestamp) of the last tick, or None. No network, no waiting."""
        with self._lock:
            return self._ticks.get(symbol.removeprefix(PREFIX))

    # ---- background connection ----
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._run, name="kraken-ws", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop = True
        self._kick()
        self._http.close()

    def _kick(self) -> None:
        """Drop the connection so the loop reconnects (used when the symbol list changes)."""
        if self._loop and self._ws:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        import websockets

        self._loop = asyncio.get_running_loop()
        backoff = 1.0
        while not self._stop:
            try:
                async with websockets.connect(WS_URL, open_timeout=15, ping_interval=20) as ws:
                    self._ws = ws
                    with self._lock:
                        symbols = sorted(self._symbols)
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "params": {"channel": "ticker", "symbol": symbols, "event_trigger": "trades"},
                    }))
                    backoff = 1.0
                    async for raw in ws:
                        self.handle(raw)
            except Exception as exc:
                log.warning("kraken websocket: %s; reconnecting in %.0fs", exc, backoff)
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
        rows = parse_ticker(msg)
        if rows:
            with self._arrived:
                for symbol, price, ts in rows:
                    self._ticks[symbol] = (price, ts)
                self._arrived.notify_all()

    # ---- Feed ----
    def _subscribe(self, symbol: str) -> None:
        with self._lock:
            new = symbol not in self._symbols
            self._symbols.add(symbol)
        if new and self._thread and self._thread.is_alive():
            self._kick()

    def _tick(self, symbol: str) -> tuple[float, float]:
        self._subscribe(symbol)
        self.start()
        deadline = time.monotonic() + FIRST_TICK_WAIT
        with self._arrived:
            while symbol not in self._ticks:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise CommodityError(f"no Kraken price yet for {symbol}")
                self._arrived.wait(left)
            return self._ticks[symbol]

    def _history(self, symbol: str) -> tuple[list[float], list[float]]:
        hit = self._bars.get(symbol)
        if hit and time.time() - hit[0] < BARS_TTL:
            return hit[1], hit[2]
        try:
            # The v2 stream names bitcoin BTC; the REST pair names still use XBT.
            pair = symbol.replace("BTC/", "XBT/").replace("/", "")
            resp = self._http.get(OHLC_URL, params={"pair": pair, "interval": 5})
            resp.raise_for_status()
            body = resp.json()
            if body.get("error"):
                raise ValueError(body["error"])
            rows = next(v for k, v in body["result"].items() if k != "last")
            times = [float(r[0]) for r in rows]
            closes = [float(r[4]) for r in rows]
        except (httpx.HTTPError, ValueError, KeyError, StopIteration, TypeError) as exc:
            raise CommodityError(f"Kraken history failed for {symbol}: {exc}") from exc
        self._bars[symbol] = (time.time(), closes, times)
        return closes, times

    def series(self, symbol: str) -> Series:
        sym = symbol.removeprefix(PREFIX)
        price, ts = self._tick(sym)
        closes, times = self._history(sym)
        return Series(last=price, last_ts=ts, closes=closes, times=times)


def _kraken_minutes(feed: KrakenFeed, symbol: str, hours: float = 4.0) -> dict[int, float]:
    sym = symbol.removeprefix(PREFIX)
    hit = feed._minutes.get(sym)
    if hit and time.time() - hit[0] < BARS_TTL:
        return hit[1]
    pair = sym.replace("BTC/", "XBT/").replace("/", "")
    try:
        resp = feed._http.get(OHLC_URL, params={"pair": pair, "interval": 1})
        resp.raise_for_status()
        body = resp.json()
        if body.get("error"):
            raise ValueError(body["error"])
        rows = next(v for k, v in body["result"].items() if k != "last")
        out = {int(float(r[0])): float(r[4]) for r in rows}
    except (httpx.HTTPError, ValueError, KeyError, StopIteration, TypeError) as exc:
        raise CommodityError(f"Kraken minutes failed for {sym}: {exc}") from exc
    feed._minutes[sym] = (time.time(), out)
    return out


class RoutingFeed:
    """Sends "kraken:" symbols to Kraken and everything else to the fallback feed."""

    def __init__(
        self, kraken: KrakenFeed | None = None, other: Feed | None = None,
        hyperliquid: Any = None,
    ):
        self.kraken = kraken or KrakenFeed()
        self._other = other
        self._hl = hyperliquid

    @classmethod
    def for_instruments(cls) -> RoutingFeed:
        """Subscribe to every Kraken pair the packaged instruments use, up front: one connection."""
        from .commodities import load_instruments
        from .hlfeed import PREFIX as HL_PREFIX
        from .hlfeed import HyperliquidFeed

        names = [i.symbol for i in load_instruments()]
        return cls(
            KrakenFeed([n for n in names if n.startswith(PREFIX)]),
            hyperliquid=HyperliquidFeed([n for n in names if n.startswith(HL_PREFIX)]),
        )

    @property
    def hyperliquid(self) -> Any:
        if self._hl is None:
            from .hlfeed import HyperliquidFeed

            self._hl = HyperliquidFeed()
        return self._hl

    def minute_closes(self, symbol: str, hours: float = 4.0) -> dict[int, float]:
        """1-minute closes (keyed by open time) from whichever feed owns the symbol."""
        if symbol.startswith(PREFIX):
            return self.kraken.minute_closes(symbol, hours)
        if symbol.startswith("hl:"):
            return self.hyperliquid.minute_closes(symbol, hours)
        raise CommodityError(f"no minute history for {symbol}")

    @property
    def other(self) -> Feed:
        if self._other is None:
            from .commodities import YahooFeed

            self._other = YahooFeed()
        return self._other

    def series(self, symbol: str) -> Series:
        if symbol.startswith(PREFIX):
            return self.kraken.series(symbol)
        if symbol.startswith("hl:"):
            return self.hyperliquid.series(symbol)
        return self.other.series(symbol)
