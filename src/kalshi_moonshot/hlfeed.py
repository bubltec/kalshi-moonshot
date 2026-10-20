"""Real-time commodity prices from Hyperliquid's builder markets (the "xyz" dex), no key needed.

Gold, silver, WTI, Brent, copper, natural gas, platinum and palladium trade there around the clock
with deep volume. A WebSocket `allMids` subscription gives the live mid every few seconds; history
for volatility and for matching Kalshi's 15-minute boundaries comes from the public candle endpoint.

Symbols are written "hl:xyz:GOLD" in instruments.toml. The price is a proxy for the reference Kalshi
settles on (Pyth, or an exchange settlement), so the pricer learns the offset between them instead of
assuming it: see CommodityPricer.learn_windows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Iterable
from typing import Any

import httpx

from .commodities import CommodityError, Series

log = logging.getLogger("moonshot")

WS_URL = "wss://api.hyperliquid.xyz/ws"
INFO_URL = "https://api.hyperliquid.xyz/info"
PREFIX = "hl:"
FIRST_TICK_WAIT = 10.0
BARS_TTL = 60.0


def parse_mids(msg: dict[str, Any]) -> dict[str, float]:
    """coin -> mid from an allMids message. Anything else: {}."""
    if msg.get("channel") != "allMids":
        return {}
    out: dict[str, float] = {}
    for coin, px in ((msg.get("data") or {}).get("mids") or {}).items():
        try:
            out[coin] = float(px)
        except (TypeError, ValueError):
            continue
    return out


class HyperliquidFeed:
    def __init__(self, symbols: Iterable[str] = (), http: httpx.Client | None = None):
        self._coins: set[str] = {s.removeprefix(PREFIX) for s in symbols}
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

    # ---- background connection ----
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._run, name="hyperliquid-ws", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop = True
        self._kick()
        self._http.close()

    def _kick(self) -> None:
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
                        dexes = sorted({c.split(":")[0] for c in self._coins if ":" in c})
                    for dex in dexes:
                        await ws.send(json.dumps({
                            "method": "subscribe", "subscription": {"type": "allMids", "dex": dex},
                        }))
                    backoff = 1.0
                    async for raw in ws:
                        self.handle(raw)
            except Exception as exc:
                log.warning("hyperliquid websocket: %s; reconnecting in %.0fs", exc, backoff)
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
        mids = parse_mids(msg)
        if not mids:
            return
        now = time.time()
        with self._arrived:
            for coin, px in mids.items():
                if coin in self._coins:
                    self._ticks[coin] = (px, now)
            self._arrived.notify_all()

    def latest(self, symbol: str) -> tuple[float, float] | None:
        """(price, local receive time) of the last tick, or None. No network, no waiting."""
        with self._lock:
            return self._ticks.get(symbol.removeprefix(PREFIX))

    # ---- Feed ----
    def _subscribe(self, coin: str) -> None:
        with self._lock:
            new = coin not in self._coins
            self._coins.add(coin)
        if new and self._thread and self._thread.is_alive():
            self._kick()

    def _tick(self, coin: str) -> tuple[float, float]:
        self._subscribe(coin)
        self.start()
        deadline = time.monotonic() + FIRST_TICK_WAIT
        with self._arrived:
            while coin not in self._ticks:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise CommodityError(f"no Hyperliquid price yet for {coin}")
                self._arrived.wait(left)
            return self._ticks[coin]

    def _candles(self, coin: str, interval: str, hours: float) -> list[dict[str, Any]]:
        now = int(time.time() * 1000)
        try:
            resp = self._http.post(INFO_URL, json={"type": "candleSnapshot", "req": {
                "coin": coin, "interval": interval,
                "startTime": now - int(hours * 3600_000), "endTime": now,
            }})
            resp.raise_for_status()
            rows = resp.json()
            if not isinstance(rows, list):
                raise ValueError(str(rows)[:80])
            return rows
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise CommodityError(f"Hyperliquid candles failed for {coin}: {exc}") from exc

    def series(self, symbol: str) -> Series:
        coin = symbol.removeprefix(PREFIX)
        price, ts = self._tick(coin)
        hit = self._bars.get(coin)
        if not hit or time.time() - hit[0] >= BARS_TTL:
            try:
                rows = self._candles(coin, "5m", 72)
                times = [r["t"] / 1000.0 for r in rows]
                closes = [float(r["c"]) for r in rows]
            except (KeyError, TypeError, ValueError) as exc:
                raise CommodityError(f"Hyperliquid candles malformed for {coin}: {exc}") from exc
            hit = (time.time(), closes, times)
            self._bars[coin] = hit
        return Series(last=price, last_ts=ts, closes=hit[1], times=hit[2])

    def minute_closes(self, symbol: str, hours: float = 4.0) -> dict[int, float]:
        """Close of each 1-minute candle, keyed by the unix second it opened."""
        coin = symbol.removeprefix(PREFIX)
        hit = self._minutes.get(coin)
        if hit and time.time() - hit[0] < BARS_TTL:
            return hit[1]
        try:
            out = {int(r["t"] // 1000): float(r["c"]) for r in self._candles(coin, "1m", hours)}
        except (KeyError, TypeError, ValueError) as exc:
            raise CommodityError(f"Hyperliquid candles malformed for {coin}: {exc}") from exc
        self._minutes[coin] = (time.time(), out)
        return out
