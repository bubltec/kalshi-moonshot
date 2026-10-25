"""Market data read once, shared by every lane: the discovery job and the cache it fills.

Without this, each lane scans and reviews on its own, and each of those calls Kalshi for the same markets: 17 lanes watching the same five events made 17 times the
requests, and one shared request budget (a few a second) and one worker thread were saturated, so a live lane's review waited over a minute and its evaluations
expired. Now a discovery thread lists the open markets of every series any lane uses (a few requests every few seconds, however many lanes there are), keeps them here,
and the live quote stream keeps the prices on them current. A lane's scope, review and paper evaluation read from this cache and cost almost nothing, so adding a lane adds
no requests. A live lane's order still re-reads the real market first (worker.do_evaluate), so nothing real is ever placed on a cached price.

Rows are handed out as copies: callers add fields to them and must not change what the next reader sees.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("moonshot")

QUOTE_STALE = 600.0  # a derived row older than this is never used on the strength of a fresh quote alone


def shared_for(client: Any, max_age: float, enabled: bool = True) -> Any:
    """`client.shared(max_age)` when the client has a cache, discovery is on and max_age is positive; otherwise a block that changes nothing."""
    if enabled and max_age > 0 and hasattr(client, "shared"):
        return client.shared(max_age)
    return contextlib.nullcontext()


class MarketCache:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._series: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._events: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._markets: dict[str, tuple[float, dict[str, Any]]] = {}
        self._event_series: dict[str, str] = {}
        self._ticker_series: dict[str, str] = {}
        self._quotes: dict[str, tuple[float, float, float]] = {}  # ticker -> (time, yes bid, yes ask), from the live stream
        self.hits = 0
        self.misses = 0

    # ---- filling ----
    def put_series(self, series: str, rows: list[dict[str, Any]]) -> None:
        now = self._clock()
        with self._lock:
            self._series[series] = (now, [dict(m) for m in rows])
            for m in rows:
                if m.get("event_ticker"):
                    self._event_series[m["event_ticker"]] = series
                if m.get("ticker"):
                    self._ticker_series[m["ticker"]] = series

    def put_event(self, event: str, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            self._events[event] = (self._clock(), [dict(m) for m in rows])

    def put_market(self, ticker: str, row: dict[str, Any]) -> None:
        with self._lock:
            self._markets[ticker] = (self._clock(), dict(row))

    def note_quote(self, ticker: str, bid: float | None, ask: float | None, ts: float | None = None) -> None:
        """The stream's latest YES bid and ask for a market (dollars). A missing side is ignored: the row keeps its last whole quote."""
        if bid is None or ask is None or ask <= 0:
            return
        self._quotes[ticker] = (ts if ts is not None else self._clock(), float(bid), float(ask))

    # ---- reading ----
    def _overlay(self, m: dict[str, Any], fetched: float) -> dict[str, Any]:
        out = dict(m)
        q = self._quotes.get(out.get("ticker", ""))
        if q and q[0] > fetched:
            out["yes_bid_dollars"] = f"{q[1]:.4f}"
            out["yes_ask_dollars"] = f"{q[2]:.4f}"
        return out

    def _stream_fresh(self, rows: list[dict[str, Any]], max_age: float) -> bool:
        """True when every row has a streamed quote no older than max_age: then the row's own age does not matter for its price."""
        now = self._clock()
        return bool(rows) and all((q := self._quotes.get(m.get("ticker", ""))) is not None and now - q[0] <= max_age for m in rows)

    def get_series(self, series: str, max_age: float) -> list[dict[str, Any]] | None:
        with self._lock:
            hit = self._series.get(series)
            if hit and self._clock() - hit[0] <= max_age:
                self.hits += 1
                return [self._overlay(m, hit[0]) for m in hit[1]]
            self.misses += 1
            return None

    def get_event(self, event: str, max_age: float) -> list[dict[str, Any]] | None:
        with self._lock:
            now = self._clock()
            direct = self._events.get(event)
            if direct and now - direct[0] <= max_age:
                self.hits += 1
                return [self._overlay(m, direct[0]) for m in direct[1]]
            ser = self._series.get(self._event_series.get(event, ""))
            if ser:
                rows = [m for m in ser[1] if m.get("event_ticker") == event]
                if rows and (now - ser[0] <= max_age or (now - ser[0] <= QUOTE_STALE and self._stream_fresh(rows, max_age))):
                    self.hits += 1
                    return [self._overlay(m, ser[0]) for m in rows]
            self.misses += 1
            return None

    def get_market(self, ticker: str, max_age: float) -> dict[str, Any] | None:
        with self._lock:
            now = self._clock()
            direct = self._markets.get(ticker)
            if direct and now - direct[0] <= max_age:
                self.hits += 1
                return self._overlay(direct[1], direct[0])
            ser = self._series.get(self._ticker_series.get(ticker, ""))
            if ser:
                for m in ser[1]:
                    if m.get("ticker") == ticker:
                        if now - ser[0] <= max_age or (now - ser[0] <= QUOTE_STALE and self._stream_fresh([m], max_age)):
                            self.hits += 1
                            return self._overlay(m, ser[0])
                        break
            self.misses += 1
            return None

    def stats(self) -> str:
        total = self.hits + self.misses
        return f"market cache: {self.hits:,} reads from memory, {self.misses:,} that went to Kalshi" + (f" ({self.hits / total:.0%} shared)" if total else "")


class Discovery:
    """A background thread that keeps the cache filled: every `every` seconds it lists the open markets of each series in `wanted(settings)`."""

    def __init__(self, get_client: Callable[[], Any], get_settings: Callable[[], Any], wanted: Callable[[Any], set[str]]):
        self.get_client, self.get_settings, self.wanted = get_client, get_settings, wanted  # the client can be replaced when the key or the rate limit changes
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_cycle = 0.0
        self.last_count = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="discovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def cycle(self) -> int:
        """List every wanted series once. Returns how many succeeded."""
        s = self.get_settings()
        client = self.get_client()
        wanted = sorted(self.wanted(s))
        # Spread the requests over the cycle instead of firing them back to back: a burst of listings would hold the whole request budget for seconds and every other
        # caller (a live lane's order, a stop) would queue behind it.
        gap = float(getattr(s, "discovery_seconds", 45.0)) * 0.8 / max(1, len(wanted))
        done = 0
        for series in wanted:
            if self._stop.is_set():
                break
            try:
                client.series_markets(series)  # an uncached call: it fills the cache for everyone
                done += 1
            except Exception as exc:
                log.debug("discovery: could not list %s (%s)", series, exc)
            self._stop.wait(gap)
        self.last_cycle, self.last_count = time.time(), done
        return done

    def _run(self) -> None:
        reported = time.time()
        while not self._stop.is_set():
            s = self.get_settings()
            if getattr(s, "discovery_enabled", True):
                try:
                    self.cycle()
                except Exception:
                    log.exception("discovery cycle failed")
                if time.time() - reported > 600:
                    reported = time.time()
                    cache = getattr(self.get_client(), "cache", None)
                    if cache is not None:
                        log.info("discovery: %d series listed per cycle; %s", self.last_count, cache.stats())
            self._stop.wait(max(5.0, float(getattr(s, "discovery_seconds", 45.0))))
