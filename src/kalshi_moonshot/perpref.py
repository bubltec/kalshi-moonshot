"""The reference price on Kalshi's own perpetual-futures markets, recorded as tape indexes (2026-11-11, user pointed at Kalshi's perps: "we can utilize that to assist our curve and buy/exit strategy").
Each Kalshi perp carries reference_price: Kalshi's live price for the underlying, about one reading a second, public (no key). At the 07:30 ET boundary on 2026-11-11 the reference of the gold, silver, platinum and palladium perps equalled the 15-minute markets' settlement value to the perp's rounding (studies/results/2026-11-11-perp-reference-probe.txt); copper and the energy contracts sat at a level offset (another instrument) and the coins settle on a 60 s average of another index. So this is recorded beside the feeds the curve reads now (BiQuote, CF Benchmarks), to measure over many windows before anything trades on it. Record only: places nothing.
One GET a second of the whole market list (one request covers every perp); a reading is recorded when its own timestamp moves on. Off with KALSHI_PERP_REF=0."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

log = logging.getLogger("perpref")

URL = "https://external-api.kalshi.com/trade-api/v2/margin/markets"
# tape index id -> Kalshi perp ticker. The index names the underlying, as the other feeds' ids do.
FEEDS: dict[str, str] = {
    "KP_GOLD": "KXGOLDPERP", "KP_SILVER": "KXSILVERPERP", "KP_PLATINUM": "KXPLATINUMPERP", "KP_PALLADIUM": "KXPALLADIUMPERP", "KP_COPPER": "KXCOPPERPERP",
    "KP_WTI": "KXWTIFUTURE", "KP_BRENT": "KXBRENTFUTURE", "KP_NATGAS": "KXHHGASFUTURE", "KP_SPX": "KXUS500PERP",
    "KP_BTC": "KXBTCPERP", "KP_ETH": "KXETHPERP", "KP_SOL": "KXSOLPERP", "KP_XRP": "KXXRPPERP", "KP_DOGE": "KXDOGEPERP", "KP_ADA": "KXADAPERP",
    "KP_BCH": "KXBCHPERP", "KP_BNB": "KXBNBPERP", "KP_HYPE": "KXHYPEPERP", "KP_NEAR": "KXNEARPERP", "KP_ZEC": "KXZECPERP",
}
_BY_TICKER = {ticker: index for index, ticker in FEEDS.items()}


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


class PerpRefFeed:
    def __init__(self, on_event: Callable[[dict[str, Any]], None], url: str = URL):
        self.on_event = on_event
        self.url = url
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen: dict[str, float] = {}  # index -> the source time of the last reading recorded
        self.last_event_at: float | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="perpref", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()

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
