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
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("moonshot")

HOT_MAX = 10  # most series the fast tier lists: at the default 15 s cycle that is well under one request a second of the shared budget
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
        self.fed_listeners: list[Callable[[bool], None]] = []  # called with True when the lifecycle subscription is confirmed and with False when its connection ends
        self._fed_since: float | None = None  # when the lifecycle subscription was confirmed on the open connection; None while there is none
        self._created: dict[str, dict[str, Any]] = {}  # markets announced by the stream, kept so a listing read just before one was announced does not lose it
        self.hits = 0
        self.misses = 0

    # ---- filling ----
    def put_series(self, series: str, rows: list[dict[str, Any]]) -> None:
        now = self._clock()
        with self._lock:
            have = {m.get("ticker") for m in rows}
            extra = [dict(m) for t, m in self._created.items() if m.get("series_ticker") == series and t not in have]
            self._series[series] = (now, [dict(m) for m in rows] + extra)
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

    # ---- keeping the list from the stream (2026-11-14, user: "why would anything go stale? it would just mean you're not subscribed to the correct websocket events") ----
    def feed(self, up: bool) -> None:
        """The lifecycle subscription is confirmed (True) or its connection ended (False). A listing read after the confirmation is kept current by the stream's events and does not expire; a connection that ends makes every listing unproven until it is read again."""
        with self._lock:
            self._fed_since = self._clock() if up else None
        for fn in list(self.fed_listeners):  # the listings read before a confirmation are unproven: whoever reads them wakes up and reads them again
            try:
                fn(up)
            except Exception:
                log.exception("market cache: feed listener failed")

    def fed(self) -> bool:
        """True while the lifecycle subscription is confirmed on an open connection."""
        with self._lock:
            return self._fed_since is not None

    def live(self, series: str) -> bool:
        """True when this series' listing was read after the stream's subscription was confirmed, so the stream's events have kept it current since."""
        with self._lock:
            return self._live_locked(series)

    def _live_locked(self, series: str) -> bool:
        hit = self._series.get(series)
        return self._fed_since is not None and hit is not None and hit[0] >= self._fed_since

    @staticmethod
    def _iso(v: Any) -> str:
        """A time from the stream (epoch seconds, milliseconds or text) as the ISO text the listing uses."""
        if isinstance(v, (int, float)) and v > 0:
            return datetime.fromtimestamp(v / 1000.0 if v > 1e11 else v, UTC).isoformat().replace("+00:00", "Z")
        return str(v) if v else ""

    def apply_market(self, e: dict[str, Any]) -> None:
        """One lifecycle event onto the listing: created adds the market, activated and deactivated set its status, close_date_updated moves its close, a strike sets its target, and a result or a settlement removes it."""
        ticker, kind = str(e.get("ticker") or ""), e.get("event") or e.get("kind")
        if not ticker:
            return
        series = ticker.split("-")[0]
        with self._lock:
            hit = self._series.get(series)
            rows = hit[1] if hit else None
            row = next((m for m in rows if m.get("ticker") == ticker), None) if rows is not None else None
            if kind == "created":
                new = {"ticker": ticker, "event_ticker": e.get("event_ticker") or "-".join(ticker.split("-")[:2]), "series_ticker": series, "title": e.get("title") or "", "yes_sub_title": e.get("sub") or "",
                       "rules_primary": e.get("rules") or "", "floor_strike": e.get("strike"), "cap_strike": e.get("cap"), "open_time": self._iso(e.get("open_ts")), "close_time": self._iso(e.get("close_ts")),
                       "status": "active", "market_type": "binary", "yes_bid_dollars": "0.0000", "yes_ask_dollars": "0.0000", "volume_fp": "0"}
                self._created[ticker] = new
                self._event_series[new["event_ticker"]] = series
                self._ticker_series[ticker] = series
                if rows is not None and row is None:
                    rows.append(dict(new))
                return
            if kind in {"settled", "result", "determined"}:
                self._created.pop(ticker, None)
                if rows is not None:
                    rows[:] = [m for m in rows if m.get("ticker") != ticker]
                return
            if row is None and ticker not in self._created and rows is not None and kind in {"strike", "activated", "deactivated", "close_date_updated"}:
                # 2026-11-14 live misses at 08:15 and 08:30: a new 15-minute window arrives as a strike event with no created event before it, and Kalshi's REST listing trails the stream by seconds, so neither the kept listing nor a read of it holds the window.
                # A strike names the window's market, its target and (in its ticker) its close, which is all a lane needs: the row is built from them. Any other event for a market the listing lacks makes the listing unproven, so its next read asks Kalshi.
                close = self._close_from_ticker(ticker)
                if kind == "strike" and e.get("strike") and close:
                    new = {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "series_ticker": series, "title": "", "yes_sub_title": "", "rules_primary": "", "floor_strike": e["strike"], "cap_strike": None,
                           "open_time": "", "close_time": close, "status": "active", "market_type": "binary", "yes_bid_dollars": "0.0000", "yes_ask_dollars": "0.0000", "volume_fp": "0"}
                    self._created[ticker] = new
                    self._event_series[new["event_ticker"]] = series
                    self._ticker_series[ticker] = series
                    rows.append(dict(new))
                else:
                    self._series[series] = (0.0, rows)
                return
            for m in (row, self._created.get(ticker)):
                if m is None:
                    continue
                if kind == "activated":
                    m["status"] = "active"
                elif kind == "deactivated":
                    m["status"] = "inactive"
                elif kind == "close_date_updated" and e.get("close_ts"):
                    m["close_time"] = self._iso(e["close_ts"])
                elif kind == "strike" and e.get("strike"):
                    m["floor_strike"] = e["strike"]

    @staticmethod
    def _close_from_ticker(ticker: str) -> str:
        """The close time a 15-minute market's ticker carries (KXBTC15M-26NOV140845-45: 2026-11-14 08:45 New York time) as ISO UTC text; empty when the ticker does not carry one."""
        import re
        from zoneinfo import ZoneInfo

        m = re.match(r"^[A-Z0-9]+-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})-", ticker)
        months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
        if not m or m.group(2) not in months:
            return ""
        try:
            local = datetime(2000 + int(m.group(1)), months.index(m.group(2)) + 1, int(m.group(3)), int(m.group(4)), int(m.group(5)), tzinfo=ZoneInfo("America/New_York"))
        except ValueError:
            return ""
        return local.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _open_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Rows of a stream-kept listing that are still open: one past its close time is closed whether or not its settlement has been announced yet."""
        now = self._clock()
        out = []
        for m in rows:
            try:
                if datetime.fromisoformat(str(m.get("close_time", "")).replace("Z", "+00:00")).timestamp() <= now:
                    continue
            except ValueError:
                pass
            out.append(m)
        return out

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

    def series_age(self, series: str) -> float | None:
        """Seconds since this series was last listed, or None if it never was."""
        with self._lock:
            hit = self._series.get(series)
            return None if hit is None else self._clock() - hit[0]

    def _rolled_rows(self, rows: list[dict[str, Any]], grace: float = 5.0) -> bool:
        """series_rolled for rows already in hand (the caller holds the lock)."""
        if not rows:
            return False
        now = self._clock()
        for m in rows:
            try:
                if datetime.fromisoformat(str(m.get("close_time", ""))).timestamp() > now + grace:
                    return False
            except ValueError:
                return False
        return True

    def series_rolled(self, series: str, grace: float = 5.0) -> bool:
        """True when the cached listing of this series has markets and none of them closes later than `grace` seconds from now: its window has rolled and the next one is due. An empty listing (a closed session) has not rolled, and a listing with an open window is current: prices come from the stream."""
        with self._lock:
            hit = self._series.get(series)
            if not hit or not hit[1]:
                return False
            now = self._clock()
            for m in hit[1]:
                try:
                    if datetime.fromisoformat(str(m.get("close_time", ""))).timestamp() > now + grace:
                        return False
                except ValueError:
                    return False  # a row with no readable close: do not call it rolled
            return True

    def get_series(self, series: str, max_age: float) -> list[dict[str, Any]] | None:
        with self._lock:
            hit = self._series.get(series)
            if hit and self._live_locked(series):  # kept by the stream's events: it has no age
                self.hits += 1
                return [self._overlay(m, 0.0) for m in self._open_rows(hit[1])]
            if hit and self._clock() - hit[0] <= max_age and not self._rolled_rows(hit[1]):
                self.hits += 1
                return [self._overlay(m, hit[0]) for m in hit[1]]
            self.misses += 1
            return None

    def get_event(self, event: str, max_age: float) -> list[dict[str, Any]] | None:
        with self._lock:
            now = self._clock()
            series = self._event_series.get(event, "")
            if series and self._live_locked(series):  # kept by the stream's events: it has no age
                rows = [m for m in self._open_rows(self._series[series][1]) if m.get("event_ticker") == event]
                if rows:
                    self.hits += 1
                    return [self._overlay(m, 0.0) for m in rows]
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
    """Background threads that keep the cache filled.

    Slow tier: every `discovery_seconds` it lists the open markets of each series in `wanted(settings)`, spread over the cycle.
    Fast tier: `hot(settings)` names the series that matter right now (one with an open bet, then one with a prospect on a lane's watchlist, live lanes first); those are
    listed again every `discovery_hot_seconds`, on their own thread, so a new window or a moving price in what the bot is acting on is seen in seconds. The slow tier skips a
    series the fast tier listed recently, so no market is read twice for the same freshness. Both go through the client's one rate limiter.
    """

    def __init__(self, get_client: Callable[[], Any], get_settings: Callable[[], Any], wanted: Callable[[Any], set[str]],
                 hot: Callable[[Any], Any] | None = None):
        self.get_client, self.get_settings, self.wanted = get_client, get_settings, wanted  # the client can be replaced when the key or the rate limit changes
        self.hot = hot
        self._stop = threading.Event()
        self._wake_hot = threading.Event()
        self._wake = threading.Event()  # set when the stream's subscription is confirmed: the series are read at once, not at the next tick
        self._thread: threading.Thread | None = None
        self._hot_thread: threading.Thread | None = None
        self.last_cycle = 0.0
        self.last_count = 0
        self.last_hot = 0.0
        self.last_hot_count = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        cache = getattr(self.get_client(), "cache", None)
        if cache is not None and hasattr(cache, "fed_listeners"):
            cache.fed_listeners.append(lambda up: (self._wake.set(), self._wake_hot.set()) if up else None)
        self._thread = threading.Thread(target=self._run, name="discovery", daemon=True)
        self._thread.start()
        if self.hot is not None:
            self._hot_thread = threading.Thread(target=self._run_hot, name="discovery-hot", daemon=True)
            self._hot_thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _fresh(self, client: Any, series: str, within: float) -> bool:
        """True when the series was listed less than `within` seconds ago and its listed window has not rolled. A window that has closed is not fresh: the next window must be seen the moment it opens."""
        cache = getattr(client, "cache", None)
        if cache is not None and hasattr(cache, "live"):
            if cache.live(series):
                return True  # the stream keeps it: no clock decides when it is read again
            if cache.fed():
                return False  # the stream is up and this series has not been read since: read it now
        age = cache.series_age(series) if cache is not None and hasattr(cache, "series_age") else None
        if age is None or age >= within:
            return False
        return not cache.series_rolled(series) if hasattr(cache, "series_rolled") else True

    def cycle(self) -> int:
        """List every wanted series once, except those listed within the last cycle by the fast tier. Returns how many succeeded."""
        s = self.get_settings()
        client = self.get_client()
        every = float(getattr(s, "discovery_seconds", 45.0))
        backstop = float(getattr(s, "discovery_backstop", every))
        wanted = [series for series in sorted(self.wanted(s)) if not self._fresh(client, series, backstop)]
        # Spread the requests over the cycle instead of firing them back to back: a burst of listings would hold the whole request budget for seconds and every other
        # caller (a live lane's order, a stop) would queue behind it.
        gap = every * 0.8 / max(1, len(wanted))
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

    def hot_cycle(self) -> int:
        """List the series that matter right now (at most HOT_MAX, in the order `hot` gives them) once. Returns how many succeeded."""
        if self.hot is None:
            return 0
        s = self.get_settings()
        client = self.get_client()
        every = float(getattr(s, "discovery_hot_seconds", 15.0))
        backstop = float(getattr(s, "discovery_hot_backstop", every))
        series_list = [x for x in dict.fromkeys(self.hot(s)) if not self._fresh(client, x, backstop)][:HOT_MAX]
        gap = every * 0.8 / max(1, len(series_list))
        done = 0
        for series in series_list:
            if self._stop.is_set():
                break
            try:
                client.series_markets(series)
                done += 1
            except Exception as exc:
                log.debug("discovery: could not list %s (%s)", series, exc)
            self._stop.wait(gap)
        self.last_hot, self.last_hot_count = time.time(), done
        return done

    def _wait(self, seconds: float, wake: threading.Event) -> None:
        """Until the next pass is due or the stream's subscription is confirmed, whichever comes first."""
        wake.wait(seconds)
        wake.clear()

    def _run_hot(self) -> None:
        while not self._stop.is_set():
            s = self.get_settings()
            if getattr(s, "discovery_enabled", True):
                try:
                    self.hot_cycle()
                except Exception:
                    log.exception("discovery fast cycle failed")
            self._wait(max(5.0, float(getattr(s, "discovery_hot_seconds", 15.0))), self._wake_hot)

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
            self._wait(max(5.0, float(getattr(s, "discovery_seconds", 45.0))), self._wake)
