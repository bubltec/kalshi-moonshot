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
from datetime import datetime
from typing import Any

from . import manual, runner
from .config import Settings
from .kalshi import KalshiClient, KalshiError
from .lanes import lane_specs, takes_profit
from .store import Store
from .strategy import fee_per_contract, flip, spike_hit, take_profit_hit, trail_hit

log = logging.getLogger("moonshot")

REFRESH_EVERY = 3.0  # seconds between reloads of the open-bet list
HOLD_MARGIN = 0.01  # holding must beat the sale value by this much before an exit is withheld
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
    trail_fast: float = 0.0
    trail_hold: float = 3.0
    peak: float = 0.0  # highest bid that held for trail_hold seconds
    last_bid: float = 0.0
    last_ts: float = 0.0
    bid_since: float = 0.0  # when the bid first showed its current value (a repeated quote at the same bid does not restart this)
    below_since: float | None = None  # when the bid first fell trail_drop below the peak and has stayed there
    retry_after: float = 0.0  # after a sale was checked and declined, do not look again before this time
    stop_drop: float = 0.0  # stop-loss: sell at once when the bid is this fraction under the price paid (0 = this position has none)
    stop_confirm: float = 0.0  # the bid must stay past the stop line this many seconds before the stop sells (0 = at once)
    stop_since: float | None = None  # when the bid first fell past the stop line and has stayed there
    stop_ref: float = 0.0  # price the stop is measured from when it is not the price paid (a hand-placed buy: the bid it could be sold at)
    stop_from: float = 0.0  # ... but only once this unix time is reached (an hourly market is only stopped in its last STOP_LOSS_HOURS)
    history: deque = field(default_factory=lambda: deque(maxlen=400))


def side_bid(side: str, yes_bid: float, yes_ask: float) -> float:
    """The price we could sell the contract we hold at, from a YES-book quote."""
    return yes_bid if side == "yes" else 1.0 - yes_ask


class ProfitWatcher:
    def __init__(
        self, get_settings: Callable[[], Settings], client: KalshiClient, store_factory: Callable[[], Store],
        hold_value: Callable[[Any, Store], float | None] | None = None,
    ):
        self.hold_value = hold_value  # (open bet, store) -> the model's calibrated chance this position pays, or None if unknown
        self.get_settings = get_settings
        self.client = client
        self.manual = manual.ManualPositions(client)
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
            if t.stop_confirm and not t.stop_ref and bid > 0:
                t.stop_ref = min(t.entry, bid)  # hand-placed: measure the stop from what it could be sold for, not the ask paid (the spread is not a loss)
            self._track_peak(t, bid, now)
            why = take_profit_hit(t.entry, bid, t.x, t.at) or spike_hit(
                list(t.history), now, t.entry, t.spike_cents, t.spike_seconds, t.spike_min_gain
            )
            key = (t.ticker, t.lane)
            fast = self._fast_crash(t, bid)
            reason = self._stop_hit(t, bid, now) or fast or why
            if reason and key not in self._inflight:
                self._inflight.add(key)
                self._jobs.put((t.ticker, t.lane, reason))

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
        if t.peak >= t.entry * (1 + t.trail_activate) - 1e-9 and t.peak > t.entry and bid <= t.peak - t.trail_fast + 1e-9:
            return f"peak-crash: bid {bid:.2f} is {t.peak - bid:.2f} off its {t.peak:.2f} peak (paid {t.entry:.2f}), fast exit"
        return None

    def _since(self, t: Target, bid: float, now: float) -> float | None:
        if t.trail_drop > 0 and trail_hit(t.entry, t.peak, bid, t.trail_activate, t.trail_drop):
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
            if stop_on:
                fresh[(ticker, lane)].stop_drop = s.stop_loss_drop
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
            fresh[key] = old

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
            if why.startswith(("peak-crash", "stop-loss")) and self.hold_value is not None:
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
            heal = (t.peak - t.trail_fast) if (t is not None and "fast exit" in why) else (t.peak - t.trail_drop if t is not None else 0.0)
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
        floor = pos.entry * (1 + s.manual_exit_min_profit) + fee_per_contract(bid)
        if not stop and bid <= floor:  # a profit-take waits for at least the minimum profit
            if floor >= 1.0 and not getattr(self, "_warned_floor", False):
                self._warned_floor = True
                log.warning("watcher: MANUAL_EXIT_MIN_PROFIT=%.2f needs a bid of %.2f for %s, which is above $1: it can never sell. "
                            "(0.5 is +50%%, 1.0 is double your money.)", s.manual_exit_min_profit, floor, ticker)
            self._back_off(ticker, manual.LANE)
            return
        if stop and self.hold_value is not None:
            # Same rule as the bot's own stop: a dip is not a loss while holding is worth more than selling.
            held = None
            try:
                series = ticker.split("-")[0]
                held = self.hold_value(
                    {"lane": "commodity", "side": pos.side, "close_time": m.get("close_time"), "ask": pos.entry, "ticker": ticker,
                     "event_ticker": m.get("event_ticker") or ticker.rsplit("-", 1)[0], "category": "", "series": series},
                    store,
                )
            except Exception:
                held = None
            flipped = pos.entry > 0.5 and held is not None and held <= 0.5  # bought as a favorite, now the underdog: sell
            if held is not None and not flipped and held > bid - fee_per_contract(bid) + HOLD_MARGIN:
                log.info("watcher: holding hand-placed %s: the model values it at %.2f against a bid of %.2f", ticker, held, bid)
                self._back_off(ticker, manual.LANE)
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
    """True when any enabled lane asks for a take-profit, or has a stop-loss for the socket to watch."""
    return s.manual_exit or any((takes_profit(spec) or (s.stop_loss_enabled and spec.stop_loss)) for spec in lane_specs(s).values() if spec.enabled)

