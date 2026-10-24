"""Perpetual-futures trade flow from Hyperliquid, recorded on the tape: one row per coin per second.

Futures lead spot slightly, and who is buying and selling on them is the closest thing to a leading signal for the 15-minute crypto markets.
A study of years of Binance data found that futures taker flow and the futures price running ahead of spot carry a small amount of information beyond
the price move so far (worth roughly the cost of trading it). Binance is not reachable from the US, so this records Hyperliquid's perps instead
(public WebSocket `trades` channel, no key), next to the Kalshi quotes and the BRTI ticks already on the tape. The question it exists to answer:
does the Kalshi quote already contain what the futures are doing, or does it lag it? Nothing here places an order.

A row is (second, coin, last price, taker-buy dollars, taker-sell dollars, buy trades, sell trades). `side` "B" in Hyperliquid's message is a trade in
which the buyer was the aggressor; the recorded data is checked for that on first use (net buying should go with rising prices).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Iterable
from typing import Any

log = logging.getLogger("moonshot")

WS_URL = "wss://api.hyperliquid.xyz/ws"
DEFAULT_COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")


def parse_trades(msg: dict[str, Any]) -> list[tuple[str, float, float, float, bool]]:
    """(coin, time in seconds, price, size, taker_bought) for each trade in a live `trades` message. Snapshots and anything else: []."""
    if msg.get("channel") != "trades" or msg.get("isSnapshot"):
        return []
    out = []
    for t in msg.get("data") or []:
        try:
            out.append((str(t["coin"]), float(t["time"]) / 1000.0, float(t["px"]), float(t["sz"]), t["side"] == "B"))
        except (KeyError, TypeError, ValueError):
            continue
    return out


class FuturesFlow:
    """Subscribes to the trades of each coin and hands every live trade to `sink(coin, seconds, price, size, taker_bought)`."""

    def __init__(self, coins: Iterable[str], sink: Callable[[str, float, float, float, bool], None]):
        self.coins = tuple(coins)
        self.sink = sink
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self._stop = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=lambda: asyncio.run(self._main()), name="futflow-ws", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop = True
        if self._loop and self._ws:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)

    def handle(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        for coin, ts, px, sz, bought in parse_trades(msg):
            try:
                self.sink(coin, ts, px, sz, bought)
            except Exception:
                log.exception("futflow: sink failed")

    async def _main(self) -> None:
        import websockets

        self._loop = asyncio.get_running_loop()
        backoff = 1.0
        while not self._stop:
            try:
                async with websockets.connect(WS_URL, open_timeout=15, ping_interval=20) as ws:
                    self._ws = ws
                    for coin in self.coins:
                        await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": coin}}))
                    backoff = 1.0
                    async for raw in ws:
                        self.handle(raw)
            except Exception as exc:
                log.warning("futflow websocket: %s; reconnecting in %.0fs", exc, backoff)
            self._ws = None
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
