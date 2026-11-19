"""Entry on the websocket: look at a watched market the moment its streamed quote or its settlement index moves.

2026-11-19, user: "entry is supposed to be on a thread based on the websocket". The slow scoping pass (runner.scope_lane) still decides which events are worth watching. This listener sits on the tape recorder's socket and, for a two-level lane (one with review_minutes), reviews one watched event when

  - a streamed quote on it arrives, or
  - a tick of the index that settles it arrives (the perp feed: the risk score reads the index as well as the quote, and Kalshi's quote may sit still while the index moves).
Every tick asks the entry logic; the lane's filter (price, window, spread) and the buy module's steps say yes or no, so no rule is kept in this file.

A hit runs the lane's own review on that one event (runner.review_lane: the same filters, buy-module screen, cooldown and one-per rule as the polled review) and queues the same `evaluate` jobs, so the buy module's steps, sizing, limits and paper-versus-live are untouched. A look at one event is taken no more often than ENTRY_MIN_GAP_SECONDS. The thread decides nothing about what to buy: every rule is a step of the lane's buy module.

The first version of this file (removed with the module strip, 2026-11-19, which left the lanes on timers) also carried the lane options entry_move, entry_approach and the late-trend lane; they were lane rules outside the modules and are not rebuilt here.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from . import runner
from .config import Settings
from .estimators import Estimator
from .kalshi import KalshiClient, KalshiError
from .kalshiws import index_for_series
from .lanes import LaneSpec, in_blackout, job_priority, lane_specs, tiered
from .marketdata import shared_for
from . import store as store_mod
from .store import Store

log = logging.getLogger("moonshot")



def wanted(s: Settings) -> bool:
    return any(tiered(spec) for spec in lane_specs(s).values() if spec.enabled)


def event_of(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0]


def quote_keys(ticker: str) -> set[str]:
    """Events this quote can belong to. A Kalshi ticker carries a strike suffix; a Polymarket ticker is the event."""
    return {event_of(ticker), ticker}


def lane_full(store: Store, spec: LaneSpec, keys: set[str]) -> bool:
    """Whether the lane already holds its max_open bets in markets outside `keys` (the event keys of the market being looked at). An evaluation would end in "lane full" (runner.evaluate) after the review had read the market for it: this answers first, from the store. The evaluation keeps its own check, which stays the one that decides."""
    if spec.max_open <= 0:
        return False
    held = [r["ticker"] for r in store.open_bets() if (r["lane"] or "moonshot") == spec.name]
    return sum(1 for t in held if not (quote_keys(t) & keys)) >= spec.max_open


def in_band(spec: LaneSpec, bid: float, ask: float) -> bool:
    """Whether a YES-book quote puts a side the lane may buy inside its price band."""
    lo, hi = spec.price
    if "yes" in spec.sides and ask > 0 and lo <= ask <= hi:
        return True
    return "no" in spec.sides and bid > 0 and lo <= 1.0 - bid <= hi


def in_window(spec: LaneSpec, close: str, now: datetime, series: str = "") -> bool:
    """Whether the lane may bet on this event yet / still, by minutes elapsed in its fixed-length window, and outside its blackout windows."""
    if in_blackout(spec, now, series):
        return False
    if not spec.timed_suffix:
        return True
    try:
        left = (datetime.fromisoformat(close) - now).total_seconds() / 60.0
    except (TypeError, ValueError):
        return True  # no close time to judge by: let the review's own filters decide
    lo, hi = spec.entry_elapsed
    return left > 0 and lo <= spec.timed_minutes - left <= hi


class EntryWatcher:
    def __init__(
        self, get_settings: Callable[[], Settings], client: KalshiClient, store_factory: Callable[[], Store],
        estimators: Callable[[Store], dict[str, Estimator]],
    ):
        self.get_settings = get_settings
        self.client = client
        self.client_for: Callable[[str], Any] | None = None  # the lane's venue; None keeps self.client for every lane
        self.store_factory = store_factory
        self.estimators = estimators
        self.watch: dict[tuple[str, str], dict[str, Any]] = {}  # (lane, event) -> the scoping pass's info for it
        self._lock = threading.Lock()
        self._jobs: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()  # (lane, event)
        self._pending: set[tuple[str, str]] = set()
        self._last: dict[tuple[str, str], float] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.reviewed: list[str] = []  # log lines, newest last (also logged)
        self._hist: dict[str, deque[tuple[float, float, float]]] = {}  # ticker -> (time, YES bid, YES ask), the last half minute
        self._pruned = 0.0
        self._tl = threading.local()  # a store connection for each thread, opened once

    def alive(self) -> bool:
        """True while the review thread runs: the worker leaves a lane to the socket only while this holds, so a dead thread puts the timers back (2026-11-19 outage)."""
        return bool(self._threads) and not self._stop.is_set() and all(t.is_alive() for t in self._threads)

    # ---- socket side: must be fast and never raise ----
    def on_event(self, e: dict[str, Any]) -> None:
        if e.get("kind") == "index" and e.get("index_id") and e.get("value"):
            self._index_review(e)
            return
        if e.get("kind") != "quote" or e.get("bid") is None or e.get("ask") is None:
            return
        keys = quote_keys(str(e["ticker"]))
        s = self.get_settings()
        specs = lane_specs(s)
        hits = [(lane, ev, info) for (lane, ev), info in self._watched().items() if ev in keys]
        self._prune_maybe()
        hist = self._hist.setdefault(e["ticker"], deque(maxlen=400))
        hist.append((float(e["ts"]), float(e["bid"]), float(e["ask"])))
        for lane, event, info in hits:
            spec = specs.get(lane)
            if spec is None or not spec.enabled or not tiered(spec):
                continue
            self._offer(s, spec, lane, event, info)

    def _index_review(self, e: dict[str, Any]) -> None:
        """A settlement index tick (the perp feed's reading of the value the market settles on): asks the entry logic for every watched event that index settles."""
        idx = str(e["index_id"])
        s = self.get_settings()
        specs = lane_specs(s)
        for (lane, event), info in self._watched().items():
            spec = specs.get(lane)
            if spec is None or not spec.enabled or not tiered(spec) or index_for_series(str(info.get("series", ""))) != idx:
                continue
            self._offer(s, spec, lane, event, info)

    def _offer(self, s: Settings, spec: LaneSpec, lane: str, event: str, info: dict[str, Any]) -> None:
        """Every tick on a watched event asks the lane's entry logic (no price or window pre-check here: the lane's filter and its buy module's steps decide), at most once per ENTRY_MIN_GAP_SECONDS for the event."""
        key = (lane, event)
        if key in self._pending or time.time() - self._last.get(key, 0.0) < s.entry_min_gap_seconds:
            return
        self._pending.add(key)
        self._jobs.put(key)

    def _watched(self) -> dict[tuple[str, str], dict[str, Any]]:
        """The watched events, from memory: the scoping pass puts them there (Store.set_watch), a restart loads them once (start). No table read on a tick. `self.watch`, when a test or a caller sets it, wins."""
        return self.watch if self.watch else store_mod.watched_in_memory(self.get_settings().db_path)

    def _prune_maybe(self) -> None:
        """Forget old markets, run from the socket events themselves at most every 30 s: there is no timer thread."""
        now = time.time()
        if now - self._pruned >= 30.0:
            self._pruned = now
            self.prune(now)

    def prune(self, now: float | None = None) -> None:
        """Forget what belongs to markets that are over, so nothing grows with the days. A market's quote history goes two minutes after its last quote; a look marker an hour after it was made."""
        now = time.time() if now is None else now
        with self._lock:
            for tk in [tk for tk, rows in self._hist.items() if not rows or rows[-1][0] < now - 120.0]:
                self._hist.pop(tk, None)
            for k in [k for k, when in self._last.items() if when < now - 3600.0]:
                self._last.pop(k, None)

    # ---- the review thread ----
    def _book(self, lane: str) -> Any:
        """The venue this lane trades."""
        if self.client_for is not None:
            return self.client_for(lane)
        return self.client

    def _store(self) -> Store:
        st = getattr(self._tl, "store", None)
        if st is None:
            st = self._tl.store = self.store_factory()
        return st

    def refresh(self) -> None:
        """Load the persisted watchlists into memory, once at start (a restart keeps what the last scoping pass found); after that the scoping pass keeps memory current."""
        s = self.get_settings()
        store = self.store_factory()
        for spec in lane_specs(s).values():
            if spec.enabled and tiered(spec):
                store.watch(spec.name)  # reads the table once and remembers it

    def review(self, lane: str, event: str) -> int:
        """Run the lane's review on one event and queue its evaluations. Returns how many were queued."""
        from .jobs import Jobs

        s = self.get_settings()
        spec = lane_specs(s).get(lane)
        if spec is None or not spec.enabled or not tiered(spec):
            return 0
        store = self._store()
        if lane_full(store, spec, {event}):
            return 0
        book = self._book(lane)
        with shared_for(book, s.discovery_max_age, s.discovery_enabled):  # the listing (strike, status) is read from the shared cache, not asked of Kalshi on every price move
            shots = runner.review_lane(s, book, store, spec, self.estimators(store), only={event})
        jobs = Jobs(store)
        every = (spec.review_minutes or 2.0) * 60
        queued = 0
        for shot in shots:
            made = jobs.enqueue(
                "evaluate", {"shot": asdict(shot)}, dedupe_key=f"evaluate:{shot.lane}:{shot.ticker}",
                expires_in=max(60.0, 2 * every), max_attempts=s.job_max_attempts, priority=job_priority(s, spec),
            )
            queued += 1 if made else 0
        if shots:
            line = f"{lane} {event}: socket tick in range, queued {queued} of {len(shots)} evaluation(s)"
            log.info("entry watcher: %s", line)
            self.reviewed.append(line)
            del self.reviewed[:-200]
        return queued

    def _run_reviews(self) -> None:
        while not self._stop.is_set():
            try:
                key = self._jobs.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._last[key] = time.time()
                self.review(*key)
            except KalshiError as exc:
                log.warning("entry watcher: could not review %s (%s)", key[1], exc)
            except Exception:
                log.exception("entry watcher: review of %s failed", key[1])
            finally:
                self._pending.discard(key)

    def start(self) -> None:
        if self._threads:
            return
        try:
            self.refresh()
        except Exception:
            log.exception("entry watcher: could not load the saved watchlists")
        for fn, name in ((self._run_reviews, "entry-review"),):
            th = threading.Thread(target=fn, name=name, daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._stop.set()
