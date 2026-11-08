"""Look at a watched market the moment its streamed quote moves into a lane's buying range.

A 15-minute contract can cross into its price band and out again between two polled reviews. The slow scoping pass
still decides which events are worth watching; this listener sits on the tape recorder's socket and, for a lane that sets
`entry_socket = true`, notices a quote that puts one side of a watched event inside the lane's price band and entry window.

A hit runs the lane's own review on that one event (runner.review_lane: the same filters, estimator, cooldown and
one-per rule as the polled review) and queues the same `evaluate` jobs, so sizing, risk limits and paper-versus-live
are untouched. The polled review keeps running as the fallback if the socket drops.

A lane with `entry_approach` also rests a buy at its price floor while a side is climbing toward that floor. The bid
sits until it fills or `entry_arm_seconds` runs out, and Kalshi cancels it then. Nothing is spent unless it fills.
Nothing here starts unless a tiered lane sets `entry_socket`.
"""

from __future__ import annotations

import bisect
import logging
import math
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from . import runner
from .config import Settings
from .estimators import Estimator
from .kalshi import KalshiClient, KalshiError
from .kalshiws import index_for_series
from .lanes import LaneSpec, in_blackout, lane_specs, tiered
from .marketdata import shared_for
from .store import Store
from .strategy import LongShot

log = logging.getLogger("moonshot")

REFRESH_EVERY = 3.0  # seconds between reloads of the watched events
GAIN_LOOKBACK = (5.0, 25.0)  # a side is gaining when its mid is above what it was between this many seconds ago


def wanted(s: Settings) -> bool:
    return any(spec.entry_socket and tiered(spec) for spec in lane_specs(s).values() if spec.enabled)


def event_of(ticker: str) -> str:
    return ticker.rsplit("-", 1)[0]


def quote_keys(ticker: str) -> set[str]:
    """Events this quote can belong to. A Kalshi ticker carries a strike suffix; a Polymarket ticker is the event."""
    return {event_of(ticker), ticker}


def lane_full(store: Store, spec: LaneSpec, keys: set[str]) -> bool:
    """Whether the lane already holds its max_open bets in markets outside `keys` (the event keys of the market being looked at).

    An evaluation then ends in "lane full" (runner.evaluate), after the review has read the market for it: this answers first, from the
    store, and costs no request. The evaluation keeps its own check, which stays the one that decides."""
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
        self._armed: dict[tuple[str, str, str], dict[str, Any]] = {}  # (lane, ticker, side) -> {"t": when armed, "shot": the prepared shot or None while it is being prepared}
        self._arm_jobs: queue.SimpleQueue[tuple[Any, ...]] = queue.SimpleQueue()  # ("warm", key) / ("fire", key, shot) / ("move", key, ts, was, now)
        self._idx: dict[str, deque[tuple[float, float]]] = {}  # settlement index id -> (time, value), about a second apart: the late lane reads the whole window from it
        self._late_state: dict[tuple[str, str], dict[str, Any]] = {}  # (lane, ticker) -> the trend and the coin's usual move, fixed once at minute 12
        self._late_done: dict[tuple[str, str], float] = {}  # (lane, ticker) -> the market's close time: one look per market, the first new extreme, bought or not
        self._moved: dict[tuple[str, str], float] = {}  # (lane, ticker) -> when a move last queued a buy

    # ---- socket side: must be fast and never raise ----
    def on_event(self, e: dict[str, Any]) -> None:
        if e.get("kind") == "index" and e.get("index_id") and e.get("value"):
            self._on_index(e)
            return
        if e.get("kind") != "quote" or e.get("bid") is None or e.get("ask") is None:
            return
        keys = quote_keys(str(e["ticker"]))
        s = self.get_settings()
        specs = lane_specs(s)
        now = datetime.fromtimestamp(float(e["ts"]), UTC)
        with self._lock:
            hits = [(lane, ev, info) for (lane, ev), info in self.watch.items() if ev in keys]
        hist = self._hist.setdefault(e["ticker"], deque(maxlen=400))
        hist.append((float(e["ts"]), float(e["bid"]), float(e["ask"])))
        for lane, event, info in hits:
            spec = specs.get(lane)
            if spec is None or not spec.enabled or not spec.entry_socket:
                continue
            if spec.late_steps > 0:
                continue  # a late-trend lane buys on the index's ticks (_on_index), never on a quote entering a band
            if spec.entry_move > 0:
                self._move(e, lane, spec, info, now)  # a move lane never takes the ask: its only entry is the resting buy after a move
                continue
            if spec.entry_approach > 0 and self._approach(e, lane, spec, info, now):
                self._last[(lane, event)] = time.time()  # the armed buy is on its way: the timed path must not queue a second review
                continue
            key = (lane, event)
            if key in self._pending or time.time() - self._last.get(key, 0.0) < spec.entry_min_gap_seconds:
                continue
            if in_band(spec, float(e["bid"]), float(e["ask"])) and in_window(spec, info.get("close", ""), now, str(info.get("series", ""))):
                self._pending.add(key)
                self._jobs.put(key)

    def prune(self, now: float | None = None) -> None:
        """Forget what belongs to markets that are over, so nothing grows with the days. Run by the refresh thread every few seconds. A market's quote history goes two minutes after its last quote, its late-trend look
        and state a few minutes after its close, and a stale arm or move marker an hour after it was made. The settlement index rings hold a fixed number of ticks."""
        now = time.time() if now is None else now
        with self._lock:
            for tk in [tk for tk, rows in self._hist.items() if not rows or rows[-1][0] < now - 120.0]:
                self._hist.pop(tk, None)
            for k in [k for k, close in self._late_done.items() if close < now - 300.0]:
                self._late_done.pop(k, None)
                self._late_state.pop(k, None)
            for k in [k for k, x in self._late_state.items() if x["close"] < now - 300.0]:
                self._late_state.pop(k, None)
            for k in [k for k, when in self._moved.items() if when < now - 3600.0]:
                self._moved.pop(k, None)
            for k in [k for k, when in self._last.items() if when < now - 3600.0]:
                self._last.pop(k, None)
            for arm_key in [key for key, arm in self._armed.items() if now - float(arm.get("t") or 0.0) > 3600.0]:
                self._armed.pop(arm_key, None)

    def _on_index(self, e: dict[str, Any]) -> None:
        """A settlement index tick: remember it and let each late-trend lane look at the markets that index settles."""
        idx, ts, v = str(e["index_id"]), float(e["ts"]), float(e["value"])
        self._idx.setdefault(idx, deque(maxlen=2400)).append((ts, v))
        specs = lane_specs(self.get_settings())
        with self._lock:
            hits = [(lane, info) for (lane, _ev), info in self.watch.items()]
        for lane, info in hits:
            spec = specs.get(lane)
            if spec is not None and spec.enabled and spec.late_steps > 0 and index_for_series(str(info.get("series", ""))) == idx:
                self._late(lane, spec, info, idx, ts, v)

    def _late_prepare(self, idx: str, spec: LaneSpec, close: float) -> dict[str, Any] | None:
        """The trend over minutes 9-12 (a vote over `late_steps` equal steps) and the coin's usual 1 s move over the same span. None when the index has no ticks that far back (the bot started late)."""
        ticks = list(self._idx.get(idx, ()))
        ts = [t for t, _ in ticks]

        def at(t: float) -> float | None:
            i = bisect.bisect_right(ts, t) - 1
            return ticks[i][1] if i >= 0 and t - ts[i] <= 5.0 else None

        start, end = int(close - 360), int(close - 180)
        pts = [at(start + 180.0 * k / spec.late_steps) for k in range(spec.late_steps + 1)]
        secs = [at(t) for t in range(start, end + 1)]
        if any(p is None for p in pts) or sum(1 for x in secs if x is None) > 20:
            return None
        rets = [math.log(b / a) for a, b in pairwise(secs) if a and b]
        if len(rets) < 100:
            return None
        mean = sum(rets) / len(rets)
        sigma = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5
        if sigma <= 0:
            return None
        steps = [p for p in pts if p is not None]
        ups = sum(1 for a, b in pairwise(steps) if b > a)
        downs = sum(1 for a, b in pairwise(steps) if b < a)
        need = spec.late_agree or spec.late_steps // 2 + 1
        trend = "up" if ups >= need and ups > downs else "down" if downs >= need and downs > ups else "mixed"
        return {"trend": trend, "sigma": sigma, "ups": ups, "downs": downs, "close": close}

    def _late(self, lane: str, spec: LaneSpec, info: dict[str, Any], idx: str, ts: float, v: float) -> None:
        """Between late_from_minute and late_to_minute: the first index tick that sets a new high of the whole window with a trend up and the index over the strike (or a new low, trend down, under it) is the signal. It buys when the distance
        from the strike, in the coin's usual moves over the time left, is at least late_min_z. The first such tick ends the look at this market whether it buys or not (the replay's rule)."""
        ticker, raw_strike = str(info.get("ticker") or ""), info.get("strike")
        try:
            close = datetime.fromisoformat(str(info.get("close"))).timestamp()
            if raw_strike is None:
                raise TypeError
            strike = float(raw_strike)
        except (TypeError, ValueError):
            return
        key = (lane, ticker)
        minute = (ts - (close - 900.0)) / 60.0
        if not ticker or key in self._late_done or not spec.late_from_minute <= minute <= spec.late_to_minute:
            return
        st = self._late_state.get(key)
        if st is None:
            st = self._late_prepare(idx, spec, close)
            if st is None or st["trend"] == "mixed":
                self._late_done[key] = close
                log.info("entry watcher: %s %s: %s", lane, ticker, "no index history for the trend" if st is None else f"mixed trend ({st['ups']} up, {st['downs']} down), passed")
                return
            self._late_state[key] = st
        prior = [x for t, x in self._idx[idx] if close - 900.0 <= t < ts]
        if not prior:
            return
        if st["trend"] == "up" and v > max(prior) and v > strike:
            side = "yes"
        elif st["trend"] == "down" and v < min(prior) and v < strike:
            side = "no"
        else:
            return
        self._late_done[key] = close
        z = math.log(v / strike) / (st["sigma"] * math.sqrt(max(close - ts, 1.0)))
        sig = {"trend": st["trend"], "ups": st["ups"], "downs": st["downs"], "z": round(z, 2), "index": v, "strike": strike, "minute": round(minute, 2), "ts": round(ts, 3)}
        if abs(z) < spec.late_min_z:
            log.info("entry watcher: %s %s: new extreme but z %.2f is under %.2f, passed", lane, ticker, z, spec.late_min_z)
            return
        self._arm_jobs.put(("late", (lane, ticker, side), sig))

    def _late_fire(self, key: tuple[str, str, str], sig: dict[str, Any]) -> None:
        """Run the lane's review for the market and queue the buy (an ordinary evaluation: the ask, taker) for the side the signal chose."""
        from .jobs import Jobs

        lane, ticker, side = key
        s = self.get_settings()
        spec = lane_specs(s).get(lane)
        if spec is None:
            return
        store = self.store_factory()
        if lane_full(store, spec, quote_keys(ticker)):
            return
        for shot in runner.review_lane(s, self._book(lane), store, spec, self.estimators(store), only=quote_keys(ticker)):
            if shot.ticker == ticker and shot.side == side:
                made = Jobs(store).enqueue(
                    "evaluate", {"shot": asdict(shot), "late": sig}, dedupe_key=f"evaluate:{shot.lane}:{shot.ticker}",
                    expires_in=60.0, max_attempts=s.job_max_attempts, priority=spec.priority,
                )
                line = f"{lane} {ticker} {side}: new window extreme, {sig['trend']} trend ({sig['ups']} up {sig['downs']} down), z {sig['z']}; evaluation {'queued' if made else 'already queued'}"
                log.info("entry watcher: %s", line)
                self.reviewed.append(line)
                return
        log.info("entry watcher: %s %s %s: signal, but the review offered no shot (price band or spread)", lane, ticker, side)

    def _move(self, e: dict[str, Any], lane: str, spec: LaneSpec, info: dict[str, Any], now: datetime) -> None:
        """A YES mid that has moved `entry_move` within `entry_move_seconds` and is at the far end of that move: queue a resting buy of the side that fell (YES fell: buy YES; YES rose: NO fell, buy NO)."""
        ticker, ts = str(e["ticker"]), float(e["ts"])
        key = (lane, ticker)
        if time.time() - self._moved.get(key, 0.0) < max(spec.entry_min_gap_seconds, spec.entry_arm_seconds + 5.0):
            return
        mids = [(r[0], (r[1] + r[2]) / 2.0) for r in self._hist.get(ticker, ()) if ts - spec.entry_move_seconds <= r[0] <= ts]
        if len(mids) < 2:
            return
        now_mid = mids[-1][1]
        hi, lo = max(m for _, m in mids), min(m for _, m in mids)
        if hi - now_mid >= spec.entry_move - 1e-9:
            side = "yes"  # YES fell
        elif now_mid - lo >= spec.entry_move - 1e-9:
            side = "no"  # YES rose, so NO fell
        else:
            return
        if side not in spec.sides or not in_window(spec, info.get("close", ""), now, str(info.get("series", ""))):
            return
        self._moved[key] = time.time()
        self._arm_jobs.put(("move", (lane, ticker, side), ts, round(hi if side == "yes" else lo, 4), round(now_mid, 4)))

    def _move_fire(self, key: tuple[str, str, str], ts: float, was: float, now_mid: float) -> None:
        """Run the lane's review for the market, then queue the resting buy (post-only, `entry_move_below` under the fallen side's ask) as an evaluation. The price is re-read by the worker."""
        from .jobs import Jobs

        lane, ticker, side = key
        s = self.get_settings()
        spec = lane_specs(s).get(lane)
        if spec is None:
            return
        store = self.store_factory()
        if lane_full(store, spec, quote_keys(ticker)):
            return
        for shot in runner.review_lane(s, self._book(lane), store, spec, self.estimators(store), only=quote_keys(ticker)):
            if shot.ticker == ticker and shot.side == side:
                made = Jobs(store).enqueue(
                    "evaluate", {"shot": asdict(shot), "floor_bid": True, "move": {"ts": ts, "yes_was": was, "yes_now": now_mid, "queued_ts": round(time.time(), 3)}},
                    dedupe_key=f"evaluate:{shot.lane}:{shot.ticker}", expires_in=max(60.0, spec.entry_arm_seconds + 15.0), max_attempts=s.job_max_attempts, priority=spec.priority,
                )
                line = f"{lane} {ticker} {side}: YES moved {was:.2f} -> {now_mid:.2f} in {spec.entry_move_seconds:.0f}s; resting buy {spec.entry_move_below:.2f} under the ask; evaluation {'queued' if made else 'already queued'}"
                log.info("entry watcher: %s", line)
                self.reviewed.append(line)
                return

    def _gaining(self, ticker: str, side: str, ts: float) -> bool:
        """Whether the side's mid is above where it was 5-25 seconds ago (the side's own terms: a NO side gains when YES falls)."""
        hist = self._hist.get(ticker)
        if not hist:
            return False

        def mid(row: tuple[float, float, float]) -> float:
            m = (row[1] + row[2]) / 2.0
            return m if side == "yes" else 1.0 - m

        old = [r for r in hist if ts - GAIN_LOOKBACK[1] <= r[0] <= ts - GAIN_LOOKBACK[0]]
        return bool(old) and mid(hist[-1]) > mid(old[-1])

    def _approach(self, e: dict[str, Any], lane: str, spec: LaneSpec, info: dict[str, Any], now: datetime) -> bool:
        """Arm a side climbing toward the floor, and rest a buy at that floor once the ask is above it. True while a side is armed, so the taker path does not also buy.

        Within `entry_approach` under the floor, gaining, and inside the entry window: the lane's review runs early, with the price band widened down by the approach, so the shot is ready. Once the side's ask is above the floor, the bid is queued at the floor. It rests for `entry_arm_seconds` and is canceled if it does not fill. A bid at the floor while the ask is still there or lower would take the book, so that quote does not send it. An arm older than `entry_arm_seconds` is dropped."""
        lo, _hi = spec.price
        ts = float(e["ts"])
        held = False
        for side in spec.sides:
            price = float(e["ask"]) if side == "yes" else 1.0 - float(e["bid"])
            key = (lane, e["ticker"], side)
            arm = self._armed.get(key)
            if arm is not None and ts - arm["t"] > spec.entry_arm_seconds:
                del self._armed[key]
                arm = None
            if arm is not None and time.time() - float(arm.get("offered") or 0.0) >= spec.entry_min_gap_seconds and arm["shot"] is not None and price > lo + 1e-9 and in_window(spec, info.get("close", ""), now, str(info.get("series", ""))):
                arm["offered"] = time.time()
                self._arm_jobs.put(("fire", key, replace(arm["shot"], ask=round(lo, 4), bid=round(max(lo - 0.01, 0.01), 4))))
            if arm is None and max(lo - spec.entry_approach, 0.01) <= price < lo and in_window(spec, info.get("close", ""), now, str(info.get("series", ""))) and self._gaining(e["ticker"], side, ts):
                self._armed[key] = {"t": ts, "shot": None, "offered": 0.0}
                self._arm_jobs.put(("warm", key))
            if key in self._armed:
                held = True
        return held

    def _warm(self, key: tuple[str, str, str]) -> None:
        """Run the lane's review for the event now, with its price floor lowered by the approach, and keep the shot for this side."""
        lane, ticker, side = key
        s = self.get_settings()
        spec = lane_specs(s).get(lane)
        arm = self._armed.get(key)
        if spec is None or arm is None:
            return
        wide = replace(spec, price=(max(spec.price[0] - spec.entry_approach, 0.01), spec.price[1]))
        store = self.store_factory()
        if lane_full(store, spec, quote_keys(ticker)):
            return  # the arm stays, with no shot, until it times out: nothing fires, and it is not re-armed on every quote
        for shot in runner.review_lane(s, self._book(lane), store, wide, self.estimators(store), only=quote_keys(ticker)):
            if shot.ticker == ticker and shot.side == side:
                with self._lock:
                    if key in self._armed:
                        self._armed[key]["shot"] = shot
                return

    def _fire(self, key: tuple[str, str, str], shot: LongShot) -> None:
        from .jobs import Jobs

        s = self.get_settings()
        spec = lane_specs(s).get(key[0])
        if spec is None:
            return
        store = self.store_factory()
        if lane_full(store, spec, quote_keys(shot.ticker)):
            return
        made = Jobs(store).enqueue(
            "evaluate", {"shot": asdict(shot), "floor_bid": True}, dedupe_key=f"evaluate:{shot.lane}:{shot.ticker}",
            expires_in=max(60.0, spec.entry_arm_seconds + 15.0), max_attempts=s.job_max_attempts, priority=spec.priority,
        )
        line = f"{key[0]} {shot.ticker} {shot.side}: bid resting at the floor {shot.ask:.2f} for {spec.entry_arm_seconds:.0f}s; evaluation {'queued' if made else 'already queued'}"
        log.info("entry watcher: %s", line)
        self.reviewed.append(line)

    def _run_arms(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._arm_jobs.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                if job[0] == "late":
                    self._late_fire(job[1], job[2])
                elif job[0] == "move":
                    self._move_fire(job[1], job[2], job[3], job[4])
                elif job[0] == "warm":
                    self._warm(job[1])
                else:
                    self._fire(job[1], job[2])
            except KalshiError as exc:
                log.warning("entry watcher: arming %s failed (%s)", job[1], exc)
                self._armed.pop(job[1], None)
            except Exception:
                log.exception("entry watcher: arming %s failed", job[1])
                self._armed.pop(job[1], None)

    # ---- the review thread ----
    def _book(self, lane: str) -> Any:
        """The venue this lane trades. One review for every provider."""
        if self.client_for is not None:
            return self.client_for(lane)
        return self.client

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
        if lane_full(store, spec, {event}):
            return 0
        book = self._book(lane)
        with shared_for(book, s.discovery_max_age, s.discovery_enabled):  # the socket price that woke this review is in the cache's overlay; the listing itself (strike, status) is read from the shared cache, not asked of Kalshi on every price move (2026-11-07: entry-review was 55-88 GET /markets a minute and the 429s)
            shots = runner.review_lane(s, book, store, spec, self.estimators(store), only={event})
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
                self.prune()
            except Exception:
                log.exception("entry watcher: refresh failed")
            self._stop.wait(REFRESH_EVERY)

    def start(self) -> None:
        if self._threads:
            return
        for fn, name in ((self._run_refresh, "entry-refresh"), (self._run_reviews, "entry-review"), (self._run_arms, "entry-arm")):
            th = threading.Thread(target=fn, name=name, daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._stop.set()
