"""Watch open bets on Kalshi's live socket and sell the moment a take-profit level is hit.

A 15-minute contract can move 20 cents in half a minute, so polling every few minutes misses the top. The tape recorder
already holds an authenticated socket open on every 15-minute market and sees each quote change as it happens. This
listener keeps a short list of open bets whose lane asks for a take-profit (`take_profit_x`, `take_profit_at`,
`spike_cents` on the lane) and checks every incoming quote against it. On a hit it does not trust the socket alone: it
re-reads the market, confirms the bid is still there, and sells with the same exit code the periodic pass uses
(runner.exit_position: a paper sale for a paper lane, an immediate-or-cancel reduce-only order for a live one).

Nothing here starts unless a lane sets one of those fields; the default lanes set none, so live behavior is unchanged.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import runner
from .config import Settings
from .kalshi import KalshiClient, KalshiError
from .lanes import lane_specs, takes_profit
from .store import Store
from .strategy import flip, spike_hit, take_profit_hit, trail_hit

log = logging.getLogger("moonshot")

REFRESH_EVERY = 3.0  # seconds between reloads of the open-bet list
HISTORY_KEEP = 120.0  # seconds of bids kept per market, for the spike rule


@dataclass
class Target:
    ticker: str
    side: str
    entry: float  # price paid, in the side's own terms
    x: float
    at: float
    spike_cents: float
    spike_seconds: float
    trail_activate: float = 0.0
    trail_drop: float = 0.0
    peak: float = 0.0
    history: deque = field(default_factory=lambda: deque(maxlen=400))


def side_bid(side: str, yes_bid: float, yes_ask: float) -> float:
    """The price we could sell the contract we hold at, from a YES-book quote."""
    return yes_bid if side == "yes" else 1.0 - yes_ask


class ProfitWatcher:
    def __init__(self, get_settings: Callable[[], Settings], client: KalshiClient, store_factory: Callable[[], Store]):
        self.get_settings = get_settings
        self.client = client
        self.store_factory = store_factory
        self.targets: dict[str, Target] = {}
        self._lock = threading.Lock()
        self._jobs: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        self._inflight: set[str] = set()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.sold: list[str] = []  # log lines, newest last (also logged)

    # ---- socket side: must be fast and never raise ----
    def on_event(self, e: dict[str, Any]) -> None:
        if e.get("kind") != "quote":
            return
        with self._lock:
            t = self.targets.get(e["ticker"])
        if t is None or e.get("bid") is None or e.get("ask") is None:
            return
        now = float(e["ts"])
        bid = side_bid(t.side, e["bid"], e["ask"])
        t.history.append((now, bid))
        while t.history and now - t.history[0][0] > HISTORY_KEEP:
            t.history.popleft()
        t.peak = max(t.peak, bid)
        why = (
            take_profit_hit(t.entry, bid, t.x, t.at)
            or trail_hit(t.entry, t.peak, bid, t.trail_activate, t.trail_drop)
            or spike_hit(list(t.history), now, t.entry, t.spike_cents, t.spike_seconds)
        )
        if why and e["ticker"] not in self._inflight:
            self._inflight.add(e["ticker"])
            self._jobs.put((e["ticker"], why))

    # ---- housekeeping and the selling thread ----
    def refresh(self) -> None:
        """Reload which open bets to watch (new bets appear, exited ones go)."""
        s = self.get_settings()
        specs = lane_specs(s)
        store = self.store_factory()
        fresh: dict[str, Target] = {}
        for row in store.open_bets():
            spec = specs.get(row["lane"] or "moonshot")
            if spec is None or not takes_profit(spec):
                continue
            ticker = row["ticker"]
            with self._lock:
                old = self.targets.get(ticker)
            fresh[ticker] = old or Target(
                ticker, row["side"] or "yes", float(row["ask"] or 0.0), spec.take_profit_x, spec.take_profit_at,
                spec.spike_cents, spec.spike_seconds, spec.trail_activate, spec.trail_drop, float(row["ask"] or 0.0),
            )
        with self._lock:
            self.targets = fresh

    def _sell(self, ticker: str, why: str) -> None:
        s = self.get_settings()
        store = self.store_factory()
        try:
            row = next((r for r in store.open_bets() if r["ticker"] == ticker), None)
            if row is None:
                return
            m = self.client.market(ticker)
            if m.get("result") in {"yes", "no"}:
                return
            side = row["side"] or "yes"
            quoted = flip(m) if side == "no" else m
            bid = 0.0
            with contextlib.suppress(TypeError, ValueError):
                bid = float(quoted.get("yes_bid_dollars") or 0.0)
            entry = float(row["ask"] or 0.0)
            if bid <= entry:  # the socket said profit; the market no longer does. Do not sell for a loss here.
                return
            line = runner.exit_position(s, self.client, store, row, bid, why + " [live]")
            log.info("watcher: %s", line)
            self.sold.append(line)
        except KalshiError as exc:
            log.warning("watcher: could not sell %s (%s)", ticker, exc)
        finally:
            self._inflight.discard(ticker)

    def _run_sells(self) -> None:
        while not self._stop.is_set():
            try:
                ticker, why = self._jobs.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._sell(ticker, why)
            except Exception:
                log.exception("watcher: sell of %s failed", ticker)
                self._inflight.discard(ticker)

    def _run_refresh(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:
                log.exception("watcher: refresh failed")
            self._stop.wait(REFRESH_EVERY)

    def start(self) -> None:
        if self._threads:
            return
        for fn, name in ((self._run_refresh, "watch-refresh"), (self._run_sells, "watch-sell")):
            th = threading.Thread(target=fn, name=name, daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._stop.set()


def wanted(s: Settings) -> bool:
    """True when any enabled lane asks for a take-profit."""
    return any(takes_profit(spec) for spec in lane_specs(s).values() if spec.enabled)

