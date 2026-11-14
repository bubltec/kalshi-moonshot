"""The reference price on Kalshi's own perpetual-futures markets, recorded as tape indexes (2026-11-11, user pointed at Kalshi's perps: "we can utilize that to assist our curve and buy/exit strategy").
Each Kalshi perp carries reference_price: Kalshi's live price for the underlying, about one reading a second, public (no key). At the 07:30 ET boundary on 2026-11-11 the reference of the gold, silver, platinum and palladium perps equalled the 15-minute markets' settlement value to the perp's rounding (studies/results/2026-11-11-perp-reference-probe.txt); copper and the energy contracts sat at a level offset (another instrument) and the coins settle on a 60 s average of another index. So this is recorded beside the feeds the curve reads now (BiQuote, CF Benchmarks), to measure over many windows before anything trades on it. Record only: places nothing.
2026-11-13 (user: "move all the recording for the Kalshi perp to the websocket. It should be on its own threads"): the readings come from Kalshi's perps websocket (the `ticker` channel carries reference_price, at most one message a market a second), on this feed's own thread. One GET of the market list at the start gives each perp's contract size, which the socket does not send; after that nothing is polled. Without API credentials there is no socket, and the old once-a-second GET of the public list is used instead. A reading is recorded when its own timestamp moves on. Off with KALSHI_PERP_REF=0."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

log = logging.getLogger("perpref")

URL = "https://external-api.kalshi.com/trade-api/v2/margin/markets"
WS_URL = "wss://external-api-margin-ws.kalshi.com/trade-api/ws/v2/margin"  # the perps market-data socket (docs.kalshi.com/margin); needs the signed handshake
WS_URL_DEMO = "wss://external-api-margin-ws.demo.kalshi.co/trade-api/ws/v2/margin"
WS_QUIET = 30.0  # seconds without any message before the connection is dropped and opened again
# tape index id -> Kalshi perp ticker. The index names the underlying, as the other feeds' ids do.
FEEDS: dict[str, str] = {
    "KP_GOLD": "KXGOLDPERP", "KP_SILVER": "KXSILVERPERP", "KP_PLATINUM": "KXPLATINUMPERP", "KP_PALLADIUM": "KXPALLADIUMPERP", "KP_COPPER": "KXCOPPERPERP",
    "KP_WTI": "KXWTIFUTURE", "KP_BRENT": "KXBRENTFUTURE", "KP_NATGAS": "KXHHGASFUTURE", "KP_SPX": "KXUS500PERP",
    "KP_BTC": "KXBTCPERP", "KP_ETH": "KXETHPERP", "KP_SOL": "KXSOLPERP", "KP_XRP": "KXXRPPERP", "KP_DOGE": "KXDOGEPERP", "KP_ADA": "KXADAPERP",
    "KP_BCH": "KXBCHPERP", "KP_BNB": "KXBNBPERP", "KP_HYPE": "KXHYPEPERP", "KP_NEAR": "KXNEARPERP", "KP_ZEC": "KXZECPERP",
}
_BY_TICKER = {ticker: index for index, ticker in FEEDS.items()}

# 15-minute series prefix -> the Kalshi perp reference that follows it. Crypto is not here: its settlement source (the CF Benchmarks index on Kalshi's socket) is already Kalshi's own.
SERIES_INDEX = {"KXGOLD": "KP_GOLD", "KXSILVER": "KP_SILVER", "KXPLATINUM": "KP_PLATINUM", "KXPALLADIUM": "KP_PALLADIUM", "KXCOPPER": "KP_COPPER", "KXWTI": "KP_WTI", "KXBRENT": "KP_BRENT", "KXNATGAS": "KP_NATGAS", "KXINX": "KP_SPX"}


def index_for_series(series: str) -> str | None:
    """The Kalshi perp reference tape index that follows a 15-minute series, or None."""
    for prefix, index in SERIES_INDEX.items():
        if series.startswith(prefix) and series[len(prefix):] == "15M":
            return index
    return None


def enabled() -> bool:
    return os.environ.get("KALSHI_PERP_REF", "1").strip().lower() not in {"0", "off", "false", "no"}


def parse_markets(markets: list[dict[str, Any]], received: float) -> list[dict[str, Any]]:
    """The index events in one market list: one per perp we record that has a usable reference price (in the underlying's own units: reference / contract size)."""
    out: list[dict[str, Any]] = []
    for m in markets:
        index = _BY_TICKER.get(str(m.get("ticker")))
        ref = m.get("reference_price") or {}
        try:
            value = float(ref["price"]) / float(m["contract_size"])
            src = float(ref["ts_ms"])
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
        if index is None or value <= 0:
            continue
        out.append({"kind": "index", "ts": received, "index_id": index, "value": value, "avg60": None, "quarter_avg": None, "src_ms": src, "sts": None})
    return out


def contract_sizes(markets: list[dict[str, Any]]) -> dict[str, float]:
    """Perp ticker -> contract size, from the market list, for the perps we record."""
    out: dict[str, float] = {}
    for m in markets:
        ticker = str(m.get("ticker"))
        if ticker not in _BY_TICKER:
            continue
        try:
            size = float(m["contract_size"])
        except (KeyError, TypeError, ValueError):
            continue
        if size > 0:
            out[ticker] = size
    return out


def parse_ticker(raw: str | bytes | dict[str, Any], sizes: dict[str, float], received: float) -> dict[str, Any] | None:
    """The index event in one message of the perps socket's `ticker` channel, or None (an ack, a perp we do not record, no reference price, or no contract size yet). The value is in the underlying's own units: reference / contract size, as parse_markets gives."""
    try:
        d = raw if isinstance(raw, dict) else json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("type") != "ticker":
        return None
    m = d.get("msg") or {}
    ticker = str(m.get("market_ticker"))
    index, size = _BY_TICKER.get(ticker), sizes.get(ticker)
    ref = m.get("reference_price") or {}
    try:
        value = float(ref["price"]) / float(size)  # type: ignore[arg-type]
        src = float(ref["ts_ms"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    if index is None or value <= 0:
        return None
    return {"kind": "index", "ts": received, "index_id": index, "value": value, "avg60": None, "quarter_avg": None, "src_ms": src, "sts": None}


class PerpRefFeed:
    def __init__(self, on_event: Callable[[dict[str, Any]], None], url: str = URL, client: Any = None, ws_url: str | None = None):
        self.on_event = on_event
        self.url = url
        self.client = client  # a KalshiClient with API credentials: the readings then come from the perps websocket; None or no credentials: the public list is polled
        self.ws_url = ws_url or (WS_URL_DEMO if "demo" in str(getattr(client, "base_url", "")) else WS_URL)
        self.sizes: dict[str, float] = {}  # perp ticker -> contract size (from the one market-list read at the start)
        self.mode = "socket" if getattr(client, "authed", False) else "poll"
        self.connected_at: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen: dict[str, float] = {}  # index -> the source time of the last reading recorded
        self.last_event_at: float | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_socket if self.mode == "socket" else self._run, name="perpref-ws" if self.mode == "socket" else "perpref", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()

    def handle_ticker(self, raw: str | bytes | dict[str, Any], now: float | None = None) -> bool:
        """One message from the perps socket: record its reference price if it is a new reading. True when recorded."""
        now = time.time() if now is None else now
        event = parse_ticker(raw, self.sizes, now)
        if event is None or self._seen.get(event["index_id"]) == event["src_ms"]:
            return False
        self._seen[event["index_id"]] = event["src_ms"]
        self.last_event_at = now
        self.on_event(event)
        return True

    def subscription(self) -> dict[str, Any]:
        return {"id": 1, "cmd": "subscribe", "params": {"channels": ["ticker"], "market_tickers": sorted(FEEDS.values()), "send_initial_snapshot": True}}

    def _load_sizes(self) -> bool:
        """The one read of the market list: each perp's contract size. True when at least one came back."""
        try:
            with httpx.Client(timeout=httpx.Timeout(10.0)) as http:
                resp = http.get(self.url, params={"limit": 1000})
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            self.sizes.update(contract_sizes(resp.json().get("markets") or []))
        except Exception as exc:
            log.warning("kalshi perp reference feed: contract sizes not read (%s)", exc)
        return bool(self.sizes)

    def _run_socket(self) -> None:
        asyncio.run(self._socket_main())

    async def _socket_main(self) -> None:
        import websockets

        backoff = 1.0
        while not self._stop.is_set():
            if not self.sizes and not self._load_sizes():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            try:
                async with websockets.connect(self.ws_url, additional_headers=self.client.ws_headers(self.ws_url), open_timeout=15, ping_interval=20) as ws:
                    await ws.send(json.dumps(self.subscription()))
                    self.connected_at, backoff, n = time.time(), 1.0, 0
                    log.info("kalshi perp reference feed on the websocket: %d perps subscribed, %d contract sizes", len(FEEDS), len(self.sizes))
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=WS_QUIET)
                        if self.handle_ticker(raw):
                            n += 1
                            if n == 1:
                                log.info("kalshi perp reference feed: first reading from the websocket")
            except Exception as exc:
                log.warning("kalshi perp reference websocket: %s; reconnecting in %.0fs", exc or type(exc).__name__, backoff)
            self.connected_at = None
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def handle(self, markets: list[dict[str, Any]], now: float | None = None) -> int:
        """Record the readings whose own timestamp moved on (a stale reference, a closed market, is recorded once). Returns how many."""
        now = time.time() if now is None else now
        n = 0
        for event in parse_markets(markets, now):
            if self._seen.get(event["index_id"]) == event["src_ms"]:
                continue
            self._seen[event["index_id"]] = event["src_ms"]
            self.last_event_at = now
            self.on_event(event)
            n += 1
        return n

    def _run(self) -> None:
        backoff = 1.0
        announced = False
        with httpx.Client(timeout=httpx.Timeout(5.0)) as http:
            while not self._stop.is_set():
                started = time.time()
                try:
                    resp = http.get(self.url, params={"limit": 1000})
                    if resp.status_code != 200:
                        raise RuntimeError(f"HTTP {resp.status_code}")
                    n = self.handle(resp.json().get("markets") or [])
                    if not announced:
                        announced = True
                        log.info("kalshi perp reference feed on: %d of %d perps read on the first poll", n, len(FEEDS))
                    backoff = 1.0
                    self._stop.wait(max(0.0, 1.0 - (time.time() - started)))
                except Exception as exc:
                    log.warning("kalshi perp reference feed: %s; retrying in %.0fs", exc, backoff)
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 30.0)
