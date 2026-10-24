"""Look at a watched market the moment its streamed quote moves into a lane's buying range.

A 15-minute contract can cross into its price band and out again between two polled reviews. The slow scoping pass
still decides which events are worth watching; this listener sits on the tape recorder's socket and, for a lane that sets
`entry_socket = true`, notices a quote that puts one side of a watched event inside the lane's price band and entry window.

It decides nothing itself. A hit runs the lane's own review on that one event (runner.review_lane: the same filters,
estimator, cooldown and one-per rule as the polled review) and queues the same `evaluate` jobs, so sizing, risk limits and
paper-versus-live are untouched. The polled review keeps running as the fallback if the socket drops.

Nothing here starts unless a tiered lane sets `entry_socket`, and nothing here places an order.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from . import runner
from .config import Settings
from .estimators import Estimator
from .kalshi import KalshiClient, KalshiError
from .lanes import LaneSpec, lane_specs, tiered
from .store import Store

log = logging.getLogger("moonshot")

REFRESH_EVERY = 3.0  # seconds between reloads of the watched events


def wanted(s: Settings) -> bool:
    return any(spec.entry_socket and tiered(spec) for spec in lane_specs(s).values() if spec.enabled)


def event_of(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0]


def in_band(spec: LaneSpec, bid: float, ask: float) -> bool:
    """Whether a YES-book quote puts a side the lane may buy inside its price band."""
    lo, hi = spec.price
    if "yes" in spec.sides and ask > 0 and lo <= ask <= hi:
        return True
    return "no" in spec.sides and bid > 0 and lo <= 1.0 - bid <= hi


def in_window(spec: LaneSpec, close: str, now: datetime) -> bool:
    """Whether the lane may bet on this event yet / still, by minutes elapsed in its fixed-length window."""
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

    # ---- socket side: must be fast and never raise ----
    def on_event(self, e: dict[str, Any]) -> None:
        if e.get("kind") != "quote" or e.get("bid") is None or e.get("ask") is None:
            return
        event = event_of(e["ticker"])
        s = self.get_settings()
        specs = lane_specs(s)
        now = datetime.fromtimestamp(float(e["ts"]), UTC)
        with self._lock:
            hits = [(lane, info) for (lane, ev), info in self.watch.items() if ev == event]
        for lane, info in hits:
            spec = specs.get(lane)
            if spec is None or not spec.enabled or not spec.entry_socket:
                continue
            key = (lane, event)
            if key in self._pending or time.time() - self._last.get(key, 0.0) < spec.entry_min_gap_seconds:
                continue
            if in_band(spec, float(e["bid"]), float(e["ask"])) and in_window(spec, info.get("close", ""), now):
                self._pending.add(key)
                self._jobs.put(key)

    # ---- the review thread ----
    def refresh(self) -> None:
        """Reload which events each entry_socket lane is watching."""
        s = self.get_settings()
        store = self.store_factory()
        fresh: dict[tuple[str, str], dict[str, Any]] = {}
        for spec in lane_specs(s).values():
            if spec.enabled and spec.entry_socket and tiered(spec):
                for event, info in store.watch(spec.name).items():
                    fresh[(spec.name, event)] = info
        with self._lock:
            self.watch = fresh

    def review(self, lane: str, event: str) -> int:
        """Run the lane's review on one event and queue its evaluations. Returns how many were queued."""
        from .jobs import Jobs

        s = self.get_settings()
        spec = lane_specs(s).get(lane)
        if spec is None or not spec.enabled or not spec.entry_socket:
            return 0
        store = self.store_factory()
        shots = runner.review_lane(s, self.client, store, spec, self.estimators(store), only={event})
        jobs = Jobs(store)
        every = (spec.review_minutes or 2.0) * 60
        queued = 0
        for shot in shots:
            made = jobs.enqueue(
                "evaluate", {"shot": asdict(shot)}, dedupe_key=f"evaluate:{shot.lane}:{shot.ticker}",
                expires_in=max(60.0, 2 * every), max_attempts=s.job_max_attempts, priority=spec.priority,
            )
            queued += 1 if made else 0
        if shots:
            line = f"{lane} {event}: socket quote in range, queued {queued} of {len(shots)} evaluation(s)"
            log.info("entry watcher: %s", line)
            self.reviewed.append(line)
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

    def _run_refresh(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:
                log.exception("entry watcher: refresh failed")
            self._stop.wait(REFRESH_EVERY)

    def start(self) -> None:
        if self._threads:
            return
        for fn, name in ((self._run_refresh, "entry-refresh"), (self._run_reviews, "entry-review")):
            th = threading.Thread(target=fn, name=name, daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._stop.set()
