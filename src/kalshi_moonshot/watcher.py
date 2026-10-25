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
import math
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import manual, runner
from .config import Settings
from .kalshi import KalshiClient, KalshiError
from .lanes import lane_specs, stop_drop_for, takes_profit
from .store import Store
from .strategy import fee_per_contract, flip, spike_hit, take_profit_hit, trail_hit

log = logging.getLogger("moonshot")

REFRESH_EVERY = 3.0  # seconds between reloads of the open-bet list
HOLD_MARGIN = 0.01  # holding must beat the sale value by this much before an exit is withheld
RETRY_AFTER = 15.0  # seconds before a declined sale is looked at again
HISTORY_KEEP = 120.0  # seconds of bids kept per market, for the spike rule
FILL_SETTLE = 3.0  # seconds after a fill before positions are read: the bot's own bet must be on the books first, or it would look hand-placed


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
    trail_fast: float = 0.0
    trail_hold: float = 3.0
    peak: float = 0.0  # highest bid that held for trail_hold seconds
    last_bid: float = 0.0
    last_ts: float = 0.0
    bid_since: float = 0.0  # when the bid first showed its current value (a repeated quote at the same bid does not restart this)
    below_since: float | None = None  # when the bid first fell trail_drop below the peak and has stayed there
    retry_after: float = 0.0  # after a sale was checked and declined, do not look again before this time
    stop_drop: float = 0.0  # stop-loss: sell at once when the bid is this fraction under the price paid (0 = this position has none)
    unstable_gain: float = 0.0  # sell when up this fraction after fees and the bid is swinging (0 = off)
    unstable_range: float = 0.0
    unstable_seconds: float = 20.0
    hover_dip: float = 0.0  # sell at the first net profit once the bid has dipped to this price or lower since entry (0 = off)
    low: float = 1.0  # lowest bid seen since this position was opened
    stop_confirm: float = 0.0  # the bid must stay past the stop line this many seconds before the stop sells (0 = at once)
    stop_since: float | None = None  # when the bid first fell past the stop line and has stayed there
    stop_ref: float = 0.0  # price the stop is measured from when it is not the price paid (a hand-placed buy: the bid it could be sold at)
    stop_from: float = 0.0  # ... but only once this unix time is reached (an hourly market is only stopped in its last STOP_LOSS_HOURS)
    trail_progress: float = 0.0  # >0: the crash exit arms once the bid has covered this share of the way from the price paid to $1.00, instead of trail_activate
    spot_sigma: float = 0.0  # the crash exit holds while spot is more than this many sigma on the winning side of the strike (0 = off)
    trail_giveback: float = 0.0  # >0: the crash exit sells once this share of the gain (peak over price paid) is given back, instead of trail_drop (0 = the fixed drop)
    history: deque = field(default_factory=lambda: deque(maxlen=400))


def arm_gain(t: Target) -> float:
    """The gain over the price paid at which the crash exit arms: the bid has covered `trail_progress` of the distance to $1.00, or the lane's plain trail_activate."""
    if t.trail_progress > 0 and 0 < t.entry < 1:
        return (t.entry + t.trail_progress * (1.0 - t.entry)) / t.entry - 1.0
    return t.trail_activate


def crash_drop(t: Target) -> float:
    """How far under its peak the bid may fall before the crash exit counts a dip: a share of the gain made so far (trail_giveback), or the fixed trail_drop."""
    if t.trail_giveback > 0 and t.peak > t.entry > 0:
        return max(0.01, t.trail_giveback * (t.peak - t.entry))
    return t.trail_drop


def below_floor(t: Target, bid: float) -> bool:
    """The crash exit protects a gain and never sells under the price paid: below that the stop-loss takes over (its own confirmation, no spot hold).
    Only where the give-back rule is on; a research lane on the plain trailing exit keeps its old behavior."""
    return t.trail_giveback > 0 and 0 < bid < t.entry - 1e-9


def crash_fast(t: Target) -> float:
    """The drop that sells at once, with no confirm wait: never less than the dip drop itself."""
    return max(t.trail_fast, crash_drop(t)) if t.trail_giveback > 0 and t.trail_fast > 0 else t.trail_fast


def side_bid(side: str, yes_bid: float, yes_ask: float) -> float:
    """The price we could sell the contract we hold at, from a YES-book quote."""
    return yes_bid if side == "yes" else 1.0 - yes_ask


class ProfitWatcher:
    def __init__(
        self, get_settings: Callable[[], Settings], client: KalshiClient, store_factory: Callable[[], Store],
        hold_value: Callable[[Any, Store], float | None] | None = None,
        stop_model: Callable[[str, str, float, str], float | None] | None = None,
        spot_side: Callable[[str, str], float | None] | None = None,
    ):
        self.spot_side = spot_side  # (ticker, side) -> the chance, from spot against the strike alone, that this side pays; None if unknown
        self.stop_model = stop_model  # (ticker, side, bid, close time) -> the model's chance this side pays, from the window's shape; None = cannot say in time
        self.hold_value = hold_value  # (open bet, store) -> the model's calibrated chance this position pays, or None if unknown
        self.get_settings = get_settings
        self.client = client
        self.manual = manual.ManualPositions(client)
        self.store_factory = store_factory
        self.targets: dict[tuple[str, str], Target] = {}  # keyed by (ticker, lane)
        self._lock = threading.Lock()
        self._jobs: queue.SimpleQueue[tuple[str, str, str]] = queue.SimpleQueue()  # (ticker, lane, why)
        self._inflight: set[tuple[str, str]] = set()
        self.follow: Callable[[set[str]], None] | None = None  # start streaming these tickers now (the tape recorder's follow)
        self._fills: queue.SimpleQueue[tuple[float, str]] = queue.SimpleQueue()  # (fill time, ticker)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.sold: list[str] = []  # log lines, newest last (also logged)

    # ---- socket side: must be fast and never raise ----
    def on_event(self, e: dict[str, Any]) -> None:
        if e.get("kind") == "fill":
            self._on_fill(e)
            return
        if e.get("kind") != "quote" or e.get("bid") is None or e.get("ask") is None:
            return
        with self._lock:
            hits = [t for (ticker, _), t in self.targets.items() if ticker == e["ticker"]]
        for t in hits:
            now = float(e["ts"])
            bid = side_bid(t.side, e["bid"], e["ask"])
            t.history.append((now, bid))
            if bid > 0:
                t.low = min(t.low, bid)
            while t.history and now - t.history[0][0] > HISTORY_KEEP:
                t.history.popleft()
            if t.lane == manual.LANE and t.stop_confirm and not t.stop_ref and bid > 0:
                t.stop_ref = min(t.entry, bid)  # hand-placed: measure the stop from what it could be sold for, not the ask paid (the spread is not a loss)
            self._track_peak(t, bid, now)
            why = take_profit_hit(t.entry, bid, t.x, t.at) or spike_hit(
                list(t.history), now, t.entry, t.spike_cents, t.spike_seconds, t.spike_min_gain
            )
            key = (t.ticker, t.lane)
            fast = self._fast_crash(t, bid)
            reason = self._stop_hit(t, bid, now) or fast or why or self._unstable_hit(t, bid, now) or self._hover_hit(t, bid)
            if reason and key not in self._inflight:
                self._inflight.add(key)
                self._jobs.put((t.ticker, t.lane, reason))

    def _on_fill(self, e: dict[str, Any]) -> None:
        """A buy on the account (from the app or the bot): enroll the market now instead of at the next position poll. Only queues; never raises."""
        s = self.get_settings()
        if not s.manual_exit or e.get("action") not in {"buy", ""} or manual.skipped(e["ticker"], s):
            return
        self._fills.put((float(e["ts"]), e["ticker"]))

    def enroll_fills(self) -> int:
        """Handle the fills that have waited FILL_SETTLE seconds: stream their markets, then re-read positions and targets once. Returns how many markets."""
        due: set[str] = set()
        waiting: list[tuple[float, str]] = []
        while True:
            try:
                ts, ticker = self._fills.get_nowait()
            except queue.Empty:
                break
            if time.time() - ts < FILL_SETTLE:
                waiting.append((ts, ticker))  # not yet: the bot's own bookkeeping may still be catching up
            else:
                due.add(ticker)
        for item in waiting:
            self._fills.put(item)
        if not due:
            return 0
        if self.follow is not None:
            self.follow(due)
        self.manual.invalidate()
        self.refresh()
        log.info("watcher: fill on %s, enrolled in the socket watch", ", ".join(sorted(due)))
        return len(due)

    def _hover_hit(self, t: Target, bid: float) -> str | None:
        """Hovering near the strike, then a profit: the bid has been down at `hover_dip` or lower since entry and now sells for more than the cost. Take it
        before the price drifts back. A position that never dipped runs on to its target."""
        if t.hover_dip <= 0 or bid <= 0 or t.entry <= 0 or t.low > t.hover_dip + 1e-9:
            return None
        if time.time() < t.retry_after:
            return None
        cost = t.entry + fee_per_contract(t.entry)
        net = bid - runner.EXIT_SLIP - fee_per_contract(bid)
        if net <= cost:
            return None
        return f"hover: bid dipped to {t.low:.2f} and is back to {bid:.2f}, up {net / cost - 1:.0%} after fees"

    def _unstable_hit(self, t: Target, bid: float, now: float) -> str | None:
        """Up in profit while the market is unstable: the bid is at least `unstable_gain` over the cost (after the sale fee) and has swung
        `unstable_range` or more, high to low, within the last `unstable_seconds`. A profit is taken before a swing can take it back."""
        if t.unstable_gain <= 0 or t.unstable_range <= 0 or bid <= 0 or t.entry <= 0:
            return None
        if time.time() < t.retry_after:
            return None
        cost = t.entry + fee_per_contract(t.entry)
        if (bid - runner.EXIT_SLIP - fee_per_contract(bid)) < cost * (1 + t.unstable_gain):
            return None
        recent = [b for ts, b in t.history if now - ts <= t.unstable_seconds]
        if len(recent) < 2 or max(recent) - min(recent) < t.unstable_range - 1e-9:
            return None
        return f"unstable: up {(bid - runner.EXIT_SLIP - fee_per_contract(bid)) / cost - 1:.0%} after fees and the bid swung {max(recent) - min(recent):.2f} in {t.unstable_seconds:.0f}s"

    def _track_peak(self, t: Target, bid: float, now: float) -> None:
        """Update the sustained peak and how long the bid has been down from it. A bid is a peak once it has stood for trail_hold
        seconds; a quote that repeats the same bid (the ask or a size moved) does not restart that clock."""
        if not t.last_ts:
            t.bid_since = now
        elif bid != t.last_bid:
            if now - t.bid_since >= t.trail_hold:
                t.peak = max(t.peak, t.last_bid)
            t.bid_since = now
        elif now - t.bid_since >= t.trail_hold:
            t.peak = max(t.peak, bid)
        t.last_bid, t.last_ts = bid, now
        t.below_since = self._since(t, bid, now)

    def _stop_hit(self, t: Target, bid: float, now: float) -> str | None:
        """The stop-loss, on the socket: the bid is `stop_drop` under the price paid (and, for an hourly market, the last hour has begun)."""
        if t.stop_drop <= 0 or bid <= 0 or t.entry <= 0 or now < t.stop_from:
            return None
        if time.time() < t.retry_after:
            return None
        ref = t.stop_ref or t.entry
        if bid > ref * (1 - t.stop_drop) + 1e-9:
            t.stop_since = None
            return None
        if t.stop_since is None:
            t.stop_since = now
        if now - t.stop_since < t.stop_confirm:
            return None  # a dip is not a loss until it lasts: wait for the price to turn
        held = f" for {now - t.stop_since:.0f}s" if t.stop_confirm else ""
        return f"stop-loss: bid {bid:.2f} is {1 - bid / ref:.0%} under the {ref:.2f} {'mark' if t.stop_ref else 'paid'}{held} (socket)"

    def _fast_crash(self, t: Target, bid: float) -> str | None:
        """A gap down: the position was armed (up trail_activate on a held peak) and the bid is now trail_fast below that peak."""
        if t.trail_fast <= 0 or t.trail_drop <= 0 or bid <= 0 or time.time() < t.retry_after:
            return None
        if below_floor(t, bid):
            return None
        if t.peak >= t.entry * (1 + arm_gain(t)) - 1e-9 and t.peak > t.entry and bid <= t.peak - crash_fast(t) + 1e-9:
            return f"peak-crash: bid {bid:.2f} is {t.peak - bid:.2f} off its {t.peak:.2f} peak (paid {t.entry:.2f}), fast exit"
        return None

    def _since(self, t: Target, bid: float, now: float) -> float | None:
        if t.trail_drop > 0 and not below_floor(t, bid) and trail_hit(t.entry, t.peak, bid, arm_gain(t), crash_drop(t)):
            return t.below_since if t.below_since is not None else now
        return None

    def check_timers(self, now: float) -> None:
        """Run about once a second: a quiet market sends no quotes, so a held dip must also be noticed by the clock."""
        with self._lock:
            targets = list(self.targets.values())
        for t in targets:
            if not t.last_ts or (t.trail_drop <= 0 and t.stop_drop <= 0):
                continue
            if now - t.bid_since >= t.trail_hold:
                t.peak = max(t.peak, t.last_bid)
            t.below_since = self._since(t, t.last_bid, now)
            key = (t.ticker, t.lane)
            fast = self._stop_hit(t, t.last_bid, now) or self._fast_crash(t, t.last_bid)
            if fast and key not in self._inflight:
                self._inflight.add(key)
                self._jobs.put((t.ticker, t.lane, fast))
                continue
            if t.below_since is not None and now - t.below_since >= t.trail_confirm and key not in self._inflight and now >= t.retry_after:
                why = None if below_floor(t, t.last_bid) else trail_hit(t.entry, t.peak, t.last_bid, arm_gain(t), crash_drop(t))
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
            stop_on = (
                spec is not None and s.stop_loss_enabled and spec.stop_loss
                and not (row["ticker"].split("-")[0].endswith("15M") and not s.stop_loss_fast)
            )
            if spec is None or not (takes_profit(spec) or stop_on):
                continue
            ticker, lane = row["ticker"], row["lane"] or "moonshot"
            with self._lock:
                old = self.targets.get((ticker, lane))
            fresh[(ticker, lane)] = old or Target(
                ticker, lane, row["side"] or "yes", float(row["ask"] or 0.0), spec.take_profit_x, spec.take_profit_at,
                spec.spike_cents, spec.spike_seconds, spec.spike_min_gain, spec.trail_activate, spec.trail_drop, spec.trail_confirm_seconds, spec.trail_fast_drop, spec.trail_hold_seconds,
                float(row["ask"] or 0.0),
            )
            fresh[(ticker, lane)].unstable_gain = spec.unstable_gain
            fresh[(ticker, lane)].unstable_range = spec.unstable_range
            fresh[(ticker, lane)].unstable_seconds = spec.unstable_seconds
            fresh[(ticker, lane)].hover_dip = spec.hover_dip
            fresh[(ticker, lane)].trail_progress = spec.trail_progress if spec.trail_progress is not None else s.crash_progress
            fresh[(ticker, lane)].trail_giveback = spec.trail_giveback if spec.trail_giveback is not None else s.crash_giveback
            fresh[(ticker, lane)].spot_sigma = spec.trail_spot_sigma if spec.trail_spot_sigma is not None else s.crash_spot_sigma
            if stop_on:
                fresh[(ticker, lane)].stop_drop = stop_drop_for(spec, s)
                fresh[(ticker, lane)].stop_confirm = spec.stop_confirm_seconds
                try:
                    close = datetime.fromisoformat(row["close_time"]).timestamp()
                    fresh[(ticker, lane)].stop_from = close - s.stop_loss_hours * 3600
                except (TypeError, ValueError):
                    fresh[(ticker, lane)].stop_from = 0.0
        if s.manual_exit and getattr(self.client, "authed", False):
            self._add_manual(s, store, fresh)
        with self._lock:
            self.targets = fresh

    def _add_manual(self, s: Settings, store: Store, fresh: dict[tuple[str, str], Target]) -> None:
        """Positions you opened by hand get the peak-crash exit, with no stop-loss (see manual.py)."""
        for p in self.manual.positions(s, store):
            key = (p.ticker, manual.LANE)
            with self._lock:
                old = self.targets.get(key)
            if old is not None and abs(old.entry - p.entry) > 0.002:
                # You added to (or trimmed) the position: the price paid is now the average, and the stop and the profit arming follow it.
                old.entry = p.entry
                old.peak = max(old.peak, p.entry) if old.peak > p.entry else p.entry
                old.below_since = None
                old.stop_ref, old.stop_since = 0.0, None
            if old is None:
                old = Target(
                    p.ticker, manual.LANE, p.side, p.entry, 0.0, 0.0, 0.0, 30.0, 0.10, s.manual_exit_activate, s.manual_exit_drop,
                    10.0, s.manual_exit_fast, 3.0, p.entry,
                )
                self._arm_stop(s, old)
            old.trail_progress = s.crash_progress
            old.trail_giveback = s.crash_giveback
            old.spot_sigma = s.crash_spot_sigma
            old.unstable_gain = s.manual_exit_unstable_gain
            old.unstable_range = s.manual_exit_unstable_range
            old.unstable_seconds = s.manual_exit_unstable_seconds
            self._cheap_rules(s, old)
            fresh[key] = old

    @staticmethod
    def _cheap_rules(s: Settings, t: Target) -> None:
        """A hand-placed buy of a cheap contract (MANUAL_CHEAP_MAX or under: a moonshot) is let ride. It has no stop and is never sold under the price paid; it sells at once at
        MANUAL_CHEAP_TAKE_X times the price paid, and otherwise only to protect a profit: once the bid has been MANUAL_CHEAP_ARM_X times the price paid, when it gives back
        MANUAL_CHEAP_GIVEBACK of the gain. Dearer positions keep the general hand-placed rules. Applied on every refresh, so adding to a position follows the new average price."""
        if 0 < t.entry <= s.manual_cheap_max:
            t.x = s.manual_cheap_take_x
            t.trail_progress = 0.0  # arm on a multiple of the price paid, not on progress toward $1.00 (a 10c buy would otherwise wait for 46c)
            t.trail_activate = s.manual_cheap_arm_x - 1.0
            t.trail_giveback = s.manual_cheap_giveback
            t.stop_drop, t.stop_since = 0.0, None  # no stop: a cheap buy is a lottery ticket, and its spread alone can look like a 30% drop
        else:
            t.x = 0.0
            t.trail_activate = s.manual_exit_activate

    def _arm_stop(self, s: Settings, t: Target) -> None:
        """The bot's own stop-loss rules for a hand-placed position: a 15-minute market only when stop_loss_fast is on, any other
        market only in its last STOP_LOSS_HOURS."""
        if s.manual_exit_stop <= 0 or (t.ticker.split("-")[0].endswith("15M") and not s.stop_loss_fast):
            return
        try:
            close = datetime.fromisoformat(str(self.client.market(t.ticker).get("close_time", ""))).timestamp()
        except (TypeError, ValueError, KalshiError):
            return  # no close time, no stop: better unwatched than stopped on a guess
        t.stop_drop = s.manual_exit_stop
        t.stop_confirm = s.manual_exit_stop_confirm
        t.stop_from = close - s.stop_loss_hours * 3600

    def manual_tickers(self) -> set[str]:
        """Hand-placed positions the socket should stream."""
        with self._lock:
            return {tk for (tk, lane) in self.targets if lane == manual.LANE}

    def _stop_guard(self, lane: str) -> bool:
        """Whether this lane's stop may be held back by the model's opinion. Only a lane that asks for it (stop_guard = true); never a hand-placed position."""
        spec = lane_specs(self.get_settings()).get(lane)
        return False if spec is None else spec.stop_guard

    def _stop_model_lane(self, lane: str) -> bool:
        """Whether this lane's stop first asks the model (stop_model = true in its settings)."""
        spec = lane_specs(self.get_settings()).get(lane)
        return spec is not None and spec.stop_model

    def _model_holds_stop(self, s: Settings, ticker: str, lane: str, side: str, entry: float, bid: float, close_time: str) -> bool:
        """A stop on a 15-minute window first asks the model (the shape prompt): True when it values the position clearly above what the sale would pay.
        Slow, unsure or no answer means sell. A favorite (bought above 50c) that the model now sees as a loser is sold too."""
        if self.stop_model is None or s.stop_model_seconds <= 0 or not ticker.split("-")[0].endswith("15M"):
            return False
        try:
            held = self.stop_model(ticker, side, bid, close_time)
        except Exception:
            held = None
        if held is None or (entry > 0.5 and held <= 0.5):
            return False
        if held > bid - fee_per_contract(bid) + HOLD_MARGIN:
            log.info("watcher: stop on %s held: the model values it at %.2f against a bid of %.2f", ticker, held, bid)
            self._back_off(ticker, lane)
            return True
        return False

    def _back_off(self, ticker: str, lane: str) -> None:
        """A declined sale is not re-checked for RETRY_AFTER seconds, so a position sitting under its price costs no API calls."""
        with self._lock:
            t = self.targets.get((ticker, lane))
        if t is not None:
            t.retry_after = time.time() + RETRY_AFTER

    def _spot_holds(self, t: Target | None, ticker: str, side: str, why: str) -> bool:
        """A crash exit protects a profit from a collapse. While spot is still comfortably on the winning side of the strike (more than
        `spot_sigma` sigma) a bid that dipped is noise, so wait and look again. Unknown spot means no opinion: the exit goes ahead."""
        if t is None or t.spot_sigma <= 0 or self.spot_side is None or not why.startswith("peak-crash"):
            return False
        try:
            p = self.spot_side(ticker, side)
        except Exception as exc:
            log.debug("watcher: no spot opinion for %s (%s)", ticker, exc)
            return False
        if p is None or p <= 0.5 * (1.0 + math.erf(t.spot_sigma / math.sqrt(2.0))):
            return False
        log.info("watcher: holding %s: spot still has this side %.0f%% likely to pay (more than %.1f sigma on the winning side), so the dip is noise",
                 ticker, 100 * p, t.spot_sigma)
        self._back_off(ticker, t.lane)
        return True

    def _sell(self, ticker: str, lane: str, why: str) -> None:
        s = self.get_settings()
        store = self.store_factory()
        try:
            if lane == manual.LANE:
                self._sell_manual(s, store, ticker, why)
                return
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
            if bid <= entry and not why.startswith(("peak-crash", "stop-loss")):  # a profit-take never sells for a loss; a peak-crash does (it was up, and is collapsing)
                self._back_off(ticker, lane)
                return
            if bid <= 0:
                return
            with self._lock:
                tgt = self.targets.get((ticker, lane))
            if why.startswith("peak-crash") and tgt is not None and below_floor(tgt, bid):
                self._back_off(ticker, lane)  # under the price paid the stop-loss decides, not the crash exit
                return
            if self._spot_holds(tgt, ticker, side, why):
                return
            hard_stop = why.startswith("stop-loss") and not self._stop_guard(lane)  # a lane may ask for a stop that no model opinion can hold back
            if why.startswith("stop-loss") and self._stop_model_lane(lane) and self._model_holds_stop(s, ticker, lane, side, entry, bid, str(row["close_time"] or "")):
                return
            if why.startswith(("peak-crash", "stop-loss")) and self.hold_value is not None and not hard_stop:
                # A cash-out or stop is only worth it when there is a real chance of loss: if holding is worth more than the bid we
                # would sell at, the dip is noise (a near-decided contract jitters), so keep the position and look again shortly.
                try:
                    held = self.hold_value(row, store)
                except Exception:
                    held = None
                losing_side = why.startswith("stop-loss") and held is not None and entry > 0.5 and held <= 0.5  # a favorite that has flipped to the losing side: no holding
                if held is not None and not losing_side and held > bid - fee_per_contract(bid) + HOLD_MARGIN:
                    log.info("watcher: holding %s: the model values it at %.2f against a bid of %.2f, so a cash-out would give value away",
                             ticker, held, bid)
                    self._back_off(ticker, lane)
                    return
            with self._lock:
                t = self.targets.get((ticker, lane))
            if t is not None and why.startswith("spike") and bid < entry * (1 + t.spike_min_gain) - 1e-9:
                self._back_off(ticker, lane)  # the spike has faded below the minimum gain by the time we looked: keep holding
                return
            heal = (t.peak - crash_fast(t)) if (t is not None and "fast exit" in why) else (t.peak - crash_drop(t) if t is not None else 0.0)
            if t is not None and t.trail_drop > 0 and why.startswith("peak-crash") and bid > heal + 0.01:
                t.below_since = None  # the dip has already healed; keep riding
                self._back_off(ticker, lane)
                return
            # The socket is the freshest price (REST can show an older quote), so anchor the order to the lower of the two.
            if t is not None and t.last_ts and time.time() - t.last_ts < 5.0 and t.last_bid > 0:
                bid = min(bid, t.last_bid)
            slip = runner.EXIT_SLIP + (0.02 if "fast exit" in why else 0.0) + (0.03 if why.startswith("stop-loss") else 0.0)  # a collapse needs the surest fill
            line = runner.exit_position(s, self.client, store, row, bid, why + " [socket]", slip=slip)
            log.info("watcher: %s", line)
            self.sold.append(line)
        except KalshiError as exc:
            log.warning("watcher: could not sell %s (%s)", ticker, exc)
        finally:
            self._inflight.discard((ticker, lane))

    def _sell_manual(self, s: Settings, store: Store, ticker: str, why: str) -> None:
        """Sell a hand-placed position, only for a profit, and only when the account really holds it and trading is on."""
        pos = next((p for p in self.manual.positions(s, store, fresh=True) if p.ticker == ticker), None)
        if pos is None:
            return  # sold, settled, or now the bot's
        m = self.client.market(ticker)
        if m.get("result") in {"yes", "no"}:
            return
        quoted = flip(m) if pos.side == "no" else m
        bid = 0.0
        with contextlib.suppress(TypeError, ValueError):
            bid = float(quoted.get("yes_bid_dollars") or 0.0)
        with self._lock:
            t = self.targets.get((ticker, manual.LANE))
        if t is not None and t.last_ts and time.time() - t.last_ts < 5.0 and t.last_bid > 0:
            bid = min(bid, t.last_bid)  # the socket is the freshest price
        stop = why.startswith("stop-loss")
        if self._spot_holds(t, ticker, pos.side, why):
            return
        floor = pos.entry * (1 + s.manual_exit_min_profit) + fee_per_contract(bid)
        if not stop and bid <= floor:  # a profit-take waits for at least the minimum profit
            if floor >= 1.0 and not getattr(self, "_warned_floor", False):
                self._warned_floor = True
                log.warning("watcher: MANUAL_EXIT_MIN_PROFIT=%.2f needs a bid of %.2f for %s, which is above $1: it can never sell. "
                            "(0.5 is +50%%, 1.0 is double your money.)", s.manual_exit_min_profit, floor, ticker)
            self._back_off(ticker, manual.LANE)
            return
        # A stop is a hard rule: it sells whatever a model thinks the position is worth. (A model's value for a collapsed contract has been wrong,
        # for example 37c against a 17c bid on a contract that expired worthless; only the crash exit above, which protects a profit, still asks it.)
        if stop and s.manual_stop_model and self._model_holds_stop(s, ticker, manual.LANE, pos.side, pos.entry, bid, str(m.get("close_time", ""))):
            return
        slip = runner.EXIT_SLIP + (0.02 if "fast exit" in why else 0.0) + (0.03 if stop else 0.0)
        price = max(0.01, round(bid - slip, 2)) if stop else max(floor, round(bid - slip, 2))
        if not (s.mode == "trade" and (s.env != "prod" or s.allow_real_money)):
            log.info("watcher: would sell hand-placed %s %.2f %s @ %.2f (paid %.2f, trading is off). %s", ticker, pos.count, pos.side, price, pos.entry, why)
            self._back_off(ticker, manual.LANE)
            return
        sell = self.client.sell_yes_ioc if pos.side == "yes" else self.client.sell_no_ioc
        resp = sell(ticker, round(pos.count, 2), price)
        filled = float(resp.get("fill_count") or 0.0)
        avg = float(resp.get("average_fill_price") or price)
        line = f"sold hand-placed {filled:g}/{pos.count:g} {ticker} {pos.side} @ {avg:.4f} (paid {pos.entry:.2f}). {why} [socket]"
        log.info("watcher: %s", line)
        self.sold.append(line)
        if filled > 0 and stop:
            from . import reverse

            with contextlib.suppress(Exception):
                reverse.request(s, store, ticker, pos.side, manual.LANE, filled * pos.entry, why)
        if filled < pos.count - 0.005:
            self._back_off(ticker, manual.LANE)

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

    def _run_fills(self) -> None:
        while not self._stop.is_set():
            try:
                self.enroll_fills()
            except Exception:
                log.exception("watcher: could not enroll a fill")
            self._stop.wait(0.5)

    def start(self) -> None:
        if self._threads:
            return
        for fn, name in (
            (self._run_refresh, "watch-refresh"), (self._run_sells, "watch-sell"), (self._run_timers, "watch-timer"),
            (self._run_fills, "watch-fills"),
        ):
            th = threading.Thread(target=fn, name=name, daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._stop.set()


def wanted(s: Settings) -> bool:
    """True when any enabled lane asks for a take-profit, or has a stop-loss for the socket to watch."""
    return s.manual_exit or any((takes_profit(spec) or (s.stop_loss_enabled and spec.stop_loss)) for spec in lane_specs(s).values() if spec.enabled)

