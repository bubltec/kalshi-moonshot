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
import time
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
RETRY_AFTER = 15.0  # seconds before a declined sale is looked at again
HISTORY_KEEP = 120.0  # seconds of bids kept per market, for the spike rule


@dataclass
class Target:
    ticker: str
    lane: str
    side: str
    entry: float  # price paid, in the side's own terms
    x: float
    at: float
    spike_cents: float
    spike_seconds: float
    spike_min_gain: float = 0.10
    trail_activate: float = 0.0
    trail_drop: float = 0.0
    trail_confirm: float = 10.0
    trail_hold: float = 3.0
    peak: float = 0.0  # highest bid that held for trail_hold seconds
    last_bid: float = 0.0
    last_ts: float = 0.0
    below_since: float | None = None  # when the bid first fell trail_drop below the peak and has stayed there
    retry_after: float = 0.0  # after a sale was checked and declined, do not look again before this time
    history: deque = field(default_factory=lambda: deque(maxlen=400))


def side_bid(side: str, yes_bid: float, yes_ask: float) -> float:
    """The price we could sell the contract we hold at, from a YES-book quote."""
    return yes_bid if side == "yes" else 1.0 - yes_ask


class ProfitWatcher:
    def __init__(self, get_settings: Callable[[], Settings], client: KalshiClient, store_factory: Callable[[], Store]):
        self.get_settings = get_settings
        self.client = client
        self.store_factory = store_factory
        self.targets: dict[tuple[str, str], Target] = {}  # keyed by (ticker, lane)
        self._lock = threading.Lock()
        self._jobs: queue.SimpleQueue[tuple[str, str, str]] = queue.SimpleQueue()  # (ticker, lane, why)
        self._inflight: set[tuple[str, str]] = set()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.sold: list[str] = []  # log lines, newest last (also logged)

    # ---- socket side: must be fast and never raise ----
    def on_event(self, e: dict[str, Any]) -> None:
        if e.get("kind") != "quote" or e.get("bid") is None or e.get("ask") is None:
            return
        with self._lock:
            hits = [t for (ticker, _), t in self.targets.items() if ticker == e["ticker"]]
        for t in hits:
            now = float(e["ts"])
            bid = side_bid(t.side, e["bid"], e["ask"])
            t.history.append((now, bid))
            while t.history and now - t.history[0][0] > HISTORY_KEEP:
                t.history.popleft()
            self._track_peak(t, bid, now)
            why = take_profit_hit(t.entry, bid, t.x, t.at) or spike_hit(
                list(t.history), now, t.entry, t.spike_cents, t.spike_seconds, t.spike_min_gain
            )
            key = (t.ticker, t.lane)
            if why and key not in self._inflight:
                self._inflight.add(key)
                self._jobs.put((t.ticker, t.lane, why))

    def _track_peak(self, t: Target, bid: float, now: float) -> None:
        """Update the sustained peak and how long the bid has been down from it. Quotes arrive only on a change, so the
        previous bid is known to have held until this one arrived."""
        if t.last_ts and now - t.last_ts >= t.trail_hold:
            t.peak = max(t.peak, t.last_bid)
        t.last_bid, t.last_ts = bid, now
        t.below_since = self._since(t, bid, now)

    def _since(self, t: Target, bid: float, now: float) -> float | None:
        if t.trail_drop > 0 and trail_hit(t.entry, t.peak, bid, t.trail_activate, t.trail_drop):
            return t.below_since if t.below_since is not None else now
        return None

    def check_timers(self, now: float) -> None:
        """Run about once a second: a quiet market sends no quotes, so a held dip must also be noticed by the clock."""
        with self._lock:
            targets = list(self.targets.values())
        for t in targets:
            if not t.last_ts or t.trail_drop <= 0:
                continue
            if now - t.last_ts >= t.trail_hold:
                t.peak = max(t.peak, t.last_bid)
            t.below_since = self._since(t, t.last_bid, now)
            key = (t.ticker, t.lane)
            if t.below_since is not None and now - t.below_since >= t.trail_confirm and key not in self._inflight and now >= t.retry_after:
                why = trail_hit(t.entry, t.peak, t.last_bid, t.trail_activate, t.trail_drop)
                if why:
                    self._inflight.add(key)
                    self._jobs.put((t.ticker, t.lane, f"{why} for {now - t.below_since:.0f}s"))

    # ---- housekeeping and the selling thread ----
    def refresh(self) -> None:
        """Reload which open bets to watch (new bets appear, exited ones go)."""
        s = self.get_settings()
        specs = lane_specs(s)
        store = self.store_factory()
        fresh: dict[tuple[str, str], Target] = {}
        for row in store.open_bets():
            spec = specs.get(row["lane"] or "moonshot")
            if spec is None or not takes_profit(spec):
                continue
            ticker, lane = row["ticker"], row["lane"] or "moonshot"
            with self._lock:
                old = self.targets.get((ticker, lane))
            fresh[(ticker, lane)] = old or Target(
                ticker, lane, row["side"] or "yes", float(row["ask"] or 0.0), spec.take_profit_x, spec.take_profit_at,
                spec.spike_cents, spec.spike_seconds, spec.spike_min_gain, spec.trail_activate, spec.trail_drop, spec.trail_confirm_seconds, spec.trail_hold_seconds,
                float(row["ask"] or 0.0),
            )
        with self._lock:
            self.targets = fresh

    def _back_off(self, ticker: str, lane: str) -> None:
        """A declined sale is not re-checked for RETRY_AFTER seconds, so a position sitting under its price costs no API calls."""
        with self._lock:
            t = self.targets.get((ticker, lane))
        if t is not None:
            t.retry_after = time.time() + RETRY_AFTER

    def _sell(self, ticker: str, lane: str, why: str) -> None:
        s = self.get_settings()
        store = self.store_factory()
        try:
            row = next((r for r in store.open_bets() if r["ticker"] == ticker and (r["lane"] or "moonshot") == lane), None)
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
                self._back_off(ticker, lane)
                return
            with self._lock:
                t = self.targets.get((ticker, lane))
            if t is not None and t.trail_drop > 0 and why.startswith("peak-crash") and bid > t.peak - t.trail_drop + 0.01:
                t.below_since = None  # the dip has already healed; keep riding
                self._back_off(ticker, lane)
                return
            line = runner.exit_position(s, self.client, store, row, bid, why + " [socket]")
            log.info("watcher: %s", line)
            self.sold.append(line)
        except KalshiError as exc:
            log.warning("watcher: could not sell %s (%s)", ticker, exc)
        finally:
            self._inflight.discard((ticker, lane))

    def _run_sells(self) -> None:
        while not self._stop.is_set():
            try:
                ticker, lane, why = self._jobs.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._sell(ticker, lane, why)
            except Exception:
                log.exception("watcher: sell of %s failed", ticker)
                self._inflight.discard((ticker, lane))

    def _run_timers(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_timers(time.time())
            except Exception:
                log.exception("watcher: timer check failed")
            self._stop.wait(1.0)

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
        for fn, name in ((self._run_refresh, "watch-refresh"), (self._run_sells, "watch-sell"), (self._run_timers, "watch-timer")):
            th = threading.Thread(target=fn, name=name, daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._stop.set()


def wanted(s: Settings) -> bool:
    """True when any enabled lane asks for a take-profit."""
    return any(takes_profit(spec) for spec in lane_specs(s).values() if spec.enabled)

