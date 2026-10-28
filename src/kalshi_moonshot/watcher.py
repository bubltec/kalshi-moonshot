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
import json
import logging
import math
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NamedTuple

from . import buyer, manual, paperfill, runner, settlement
from .config import Settings
from .kalshi import KalshiClient, KalshiError
from .kalshiws import index_for_series
from .lanes import LaneSpec, enabled_lanes, lane_specs, stop_drop_for, takes_profit
from .store import Store
from .strategy import (
    cash_if_losing,
    cash_if_up,
    early_cash,
    fee_per_contract,
    flip,
    hard_loss,
    price_shape,
    scalp_hit,
    spike_hit,
    stop_wait,
    take_profit_hit,
    trail_hit,
    watch_trend,
)

log = logging.getLogger("moonshot")

SETTLE_WINDOW = 150.0  # the settlement guard looks at a 15-minute crypto bet in its last 150 seconds. Widened to 420 s on 2026-10-25 from a replay, but live it sold 13 positions at 200-345 s and 12 went on to win (-0.34 vs +1.55 per contract held): back to 150
BURST_RATIO = 5.0  # this SIDE's last 3 seconds of taker buys vs this side's own median second (the 2 minutes before). A pile-on at 95c is not a 5c burst
BURST_FLOOR = 300.0  # a burst must also be at least this many contracts on that side, so a handful of cheap prints never counts
BURST_MIN_NORMAL = 10.0  # 5x of a silent side is not a burst. That side must trade this many contracts in a typical second
HUNT_CLEAR_RATIO = 6.0  # a moonshot under this multiple is only a routine last-two-minutes print unless it also clears HUNT_CLEAR_FLOOR
HUNT_CLEAR_FLOOR = 500.0  # 19 of 36 moonshot calls were 5.0-5.9x; the small ones (about 300 contracts) are the loose calls. The multiple stays 5 because that band made the profit
HUNT_REPEAT_SECONDS = 8  # that many separate seconds of buying before the 3-second print
HUNT_REPEAT_GAP = 17  # the seconds just before the print, where the repeat has to show
HUNT_REPEAT_FLOOR = 20.0  # a second counts once it matches a normal second, and at least this many contracts
BURST_BAND = (0.80, 0.97)  # the held side's bid must be in this band: a favorite
BUY_BAND = (0.05, 0.25)  # the other side is bought for real only when it costs this much
SETTLE_LAST = 3.0  # ... and not in the last 3 (an order would land after the close)
REFRESH_EVERY = 3.0  # seconds between reloads of the open-bet list
HOLD_MARGIN = 0.01  # holding must beat the sale value by this much before an exit is withheld
RETRY_AFTER = 15.0  # seconds before a declined sale is looked at again
CROSS_STALE = 15.0  # a Kalshi quote older than this is not compared with a Polymarket bid
HISTORY_KEEP = 120.0  # seconds of bids kept per market, for the spike rule
FILL_SETTLE = 3.0  # seconds after a fill before positions are read: the bot's own bet must be on the books first, or it would look hand-placed


class SellSignal(NamedTuple):
    """A watcher's call to sell a held position; the sell worker acts on it. A plain 3-tuple underneath, so it unpacks as (ticker, lane, why)."""
    ticker: str
    lane: str
    why: str


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
    loss_since: float | None = None  # when the bid first reached the hard-loss line and has stayed there
    loss_low: float = 0.0  # the worst bid of that visit. A bounce of watch_loss_lift off it is held
    stop_ref: float = 0.0  # price the stop is measured from when it is not the price paid (a hand-placed buy: the bid it could be sold at)
    stop_from: float = 0.0  # ... but only once this unix time is reached (an hourly market is only stopped in its last STOP_LOSS_HOURS)
    trail_progress: float = 0.0  # >0: the crash exit arms once the bid has covered this share of the way from the price paid to $1.00, instead of trail_activate
    spot_sigma: float = 0.0  # the crash exit holds while spot is more than this many sigma on the winning side of the strike (0 = off)
    trail_giveback: float = 0.0  # >0: the crash exit sells once this share of the gain (peak over price paid) is given back, instead of trail_drop (0 = the fixed drop)
    settle_guard: float = 0.0  # sell in the last 150 s when the exact settlement chance falls this far under the bid (settlement.py; 0 = off)
    settle_flip: bool = False  # after that sale, log a paper bet on the other side (never an order)
    strike: float = 0.0  # the market's target
    close_ts: float = 0.0
    opened_min: float = 1e9  # minutes into the window when this buy was placed
    cash_before: float = 0.0  # early_cash: a buy opened before this minute is sold early (0 = off)
    cash_leave: float = 0.0
    cash_up: float = 0.0  # cash_if_up: sell once the window reaches this minute and the bid is above the price paid (0 = off)
    cash_drop: float = 0.0  # ...or this fraction under the price paid (0 = off)
    cash_left: float = 0.0  # cash_if_losing: with this many seconds left, sell a hover still under the price paid (0 = off)
    cash_at: float = 0.0
    watch_at: float = 0.0  # watch_trend: start watching once the bid prints this (0 = off)
    watch_ride: float = 0.0  # the ride at watch_patient seconds left
    watch_patient: float = 420.0
    watch_loss: float = 0.0  # hard_loss fraction of the price paid (0 = off)
    watch_loss_seconds: float = 3.0
    watch_loss_lift: float = 0.0  # hard_loss: a bounce this far off loss_low is held (0 = off)
    cross_gap: float = 0.0  # lane stop_cross_gap: skip a sale on a Polymarket bid this far under the Kalshi bid (0 = off)
    cross_logged: float = 0.0  # when the last skipped sale was logged
    scalp_at: float = 0.0  # scalp_hit: follow the high once the bid prints this price (0 = off)
    scalp_up: float = 0.0  # or once the bid is this far above the price paid (0 = off)
    scalp_dip: float = 0.03
    scalp_dip_hard: float = LaneSpec.scalp_dip_hard  # a fall this far under the high sells at once (lane scalp_dip_hard; 0 = off)
    scalp_dip_seconds: float = LaneSpec.scalp_dip_seconds  # the dip must hold this long before it sells (lane scalp_dip_seconds)
    scalp_dip_since: float | None = None  # when the bid first went scalp_dip under the high and has stayed there
    scalp_high: float = 0.0  # the highest bid since the arm printed
    index_id: str = ""  # the settlement index of this market
    settle_checked: float = 0.0  # the guard looks at most once a second
    cur_bid: float = 0.0  # the latest bid and the opposite side's ask, from the socket
    cur_other_ask: float = 0.0
    burst_buy: float = 0.0  # dollars to buy the other side with, for real, on a burst (lane burst_buy)
    burst_logged: bool = False  # one burst log per held market
    burst_share: float = LaneSpec.burst_share  # the share of the 3 s of dollars that must be against the holder for a burst to sell it (lane burst_share; 0 = off)
    burst_favorite: float = LaneSpec.burst_favorite  # the held side's bid must be at least this for a burst to sell it (lane burst_favorite)
    burst_window: float = LaneSpec.burst_window  # ...in the last this many seconds (lane burst_window)...
    burst_min_left: float = LaneSpec.burst_min_left  # ...and not in the last this many (lane burst_min_left)
    burst_bar: float = 0.0  # the dollars bought against the held favorite in 3 s that sell it (burst_dollars; 0 = the older multiple-of-normal rule)
    burst_spot_edge: float = LaneSpec.burst_spot_edge  # a burst sells only when spot's own chance for this side is not more than this far over the sale price (lane burst_spot_edge; 0 = off)
    burst_resell: bool = False  # the burst sale filled nothing: the next burst sells again (the log and the other-side buy stay once)
    flip_hint: tuple[float, float] = (0.0, 0.0)  # (the other side's ask, its chance) at the moment the guard fired
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


def _burst_category(ticker: str) -> str:
    return buyer.category(ticker)


def _side_burst(book: dict[int, list[float]], sec: int, idx: int) -> tuple[float, float, float]:
    """This side's taker contracts in the last 3 seconds, its own median second of the 2 minutes before, and that 5x-style ratio."""
    vol = 0.0
    for k in range(3):
        r = book.get(sec - k)
        if r:
            vol += r[idx]
    per = sorted(book.get(sec - 3 - k, (0.0, 0.0))[idx] for k in range(120))
    base = (per[59] + per[60]) / 2.0
    return vol, base, vol / (3.0 * base + 1.0)


def _side_dollar_ratio(book: dict[int, list[float]], sec: int, idx: int) -> float:
    """The same burst measured in DOLLARS (contracts x the price paid). A cheap side buys many more contracts per dollar as its price falls, so a side sliding from 30c to 3c bursts in contracts with no extra money behind it:
    on the moonshot's 61 settled tickets the winners' median dollar ratio was 8.7 against 3.4 for the losers (contracts: 11.3 against 9.7), and BTC's was 1.6."""
    vol = 0.0
    for k in range(3):
        r = book.get(sec - k)
        if r and len(r) > idx + 2:
            vol += r[idx + 2]
    per = sorted((book.get(sec - 3 - k) or [0.0] * 4)[idx + 2] if len(book.get(sec - 3 - k) or [0.0] * 4) > idx + 2 else 0.0 for k in range(120))
    base = (per[59] + per[60]) / 2.0
    return vol / (3.0 * base + 0.01)


def _is_side_burst(vol: float, base: float, ratio: float) -> bool:
    return ratio >= BURST_RATIO and vol >= BURST_FLOOR and base >= BURST_MIN_NORMAL


def _hunt_burst(vol: float, base: float, ratio: float) -> bool:
    """The favorite-sell burst, plus a cushion when the multiple only just clears 5x."""
    if not _is_side_burst(vol, base, ratio):
        return False
    return ratio >= HUNT_CLEAR_RATIO or vol >= HUNT_CLEAR_FLOOR


def _side_ask(side: str, yes_bid: float, yes_ask: float) -> float:
    return yes_ask if side == "yes" else 1.0 - yes_bid


def _hunt_crashing(quotes: deque[tuple[float, float, float]], side: str, now: float, ask: float, drop: float = LaneSpec.hunt_crash, lookback: float = LaneSpec.hunt_crash_seconds) -> tuple[bool, float]:
    """True when this side's ask is `drop` below the highest ask seen in the last `lookback` seconds (the lane's hunt_crash and hunt_crash_seconds).
    Returns the prior high, or 0 when there is no recent quote to compare."""
    high = 0.0
    seen = False
    for ts, yes_bid, yes_ask in quotes:
        age = now - ts
        if age < 0 or age > lookback:
            continue
        seen = True
        high = max(high, _side_ask(side, yes_bid, yes_ask))
    if not seen or high <= 0:
        return False, 0.0
    return ask <= high - drop, high


def _hunt_parked(
    quotes: deque[tuple[float, float, float]], side: str, now: float, ask: float, cap: float = LaneSpec.hunt_park_high,
    seconds: float = LaneSpec.hunt_park_seconds, floor: float = LaneSpec.hunt_park_high, span: float = LaneSpec.hunt_park_span,
) -> bool:
    """The side has been sitting cheap for the last `seconds` (the lane's hunt_park_seconds), not just printing cheap on this one quote. Parked means never above the lane's top price `cap`, or `floor` (hunt_park_high) if that is higher,
    and within `span` (hunt_park_span) of the ask we would pay."""
    high = 0.0
    oldest = -1.0
    for ts, yes_bid, yes_ask in quotes:
        age = now - ts
        if age < 0 or age > seconds:
            continue
        oldest = max(oldest, age)
        high = max(high, _side_ask(side, yes_bid, yes_ask))
    if oldest < seconds - 5.0 or high <= 0:
        return False
    return high <= max(cap, floor) and high <= ask + span


def _hunt_repeating(book: dict[int, list[float]], sec: int, idx: int, base: float) -> bool:
    """Buying already showed up in several separate seconds before this 3-second print."""
    need = max(HUNT_REPEAT_FLOOR, base)
    n = 0
    for k in range(3, 3 + HUNT_REPEAT_GAP):
        row = book.get(sec - k)
        if row and row[idx] >= need:
            n += 1
    return n >= HUNT_REPEAT_SECONDS


@dataclass
class Hunt:
    """A 15-minute market a burst_hunt lane is watching without a position."""
    ticker: str
    lane: str
    close_ts: float = 0.0
    yes_bid: float = 0.0
    yes_ask: float = 0.0
    fired: bool = False
    attempts: int = 0  # live tries that bought nothing
    retry_at: float = 0.0  # no new try before this time
    miss_row: int | None = None  # the decision row of the first miss: a later fill upgrades it to a trade instead of adding a second bet
    quotes: deque[tuple[float, float, float]] = field(default_factory=deque)  # (ts, yes bid, yes ask), last hunt_park_seconds and a bit


@dataclass
class BuySignal:
    """A watcher's call to buy; the buy worker acts on it. `kind` says which buy:
    hunt = the cheap side of a market with no position (a burst_hunt lane; `hunt` and `spec` are set, `ask` was read at the burst),
    flip = the other side of a held favorite whose burst is selling it (`held` is the target; its lane's burst_buy sets the stake)."""
    kind: str
    side: str
    ask: float
    reason: str
    ctx: dict[str, Any]
    hunt: Hunt | None = None
    spec: LaneSpec | None = None
    held: Target | None = None
    queued: float = field(default_factory=time.time)

    @property
    def ticker(self) -> str:
        return (self.hunt or self.held).ticker  # type: ignore[union-attr]


class ProfitWatcher:
    def __init__(
        self, get_settings: Callable[[], Settings], client: KalshiClient, store_factory: Callable[[], Store],
        hold_value: Callable[[Any, Store], float | None] | None = None,
        stop_model: Callable[[str, str, float, str], float | None] | None = None,
        spot_side: Callable[[str, str], float | None] | None = None,
    ):
        self.spot_side = spot_side  # (ticker, side) -> the chance, from spot against the strike alone, that this side pays; None if unknown
        self._model = settlement.Model(lambda: self.tape_factory() if self.tape_factory is not None else None)  # the exact settlement chance (settlement.py)
        self._spot = self._model.spot  # settlement index -> (time, latest value), from the socket's index ticks
        self._tls = threading.local()
        self._flow: dict[str, dict[int, list[float]]] = {}  # ticker -> second -> [YES-taker contracts, NO-taker contracts], the last 125 seconds
        self._soft_holds: dict[tuple[str, str], float] = {}  # (ticker, lane) -> when the model first said hold on a stop (not yet confirmed)
        self.stop_model = stop_model  # (ticker, side, bid, close time) -> the model's chance this side pays, from the window's shape; None = cannot say in time
        self.hold_value = hold_value  # (open bet, store) -> the model's calibrated chance this position pays, or None if unknown
        self.get_settings = get_settings
        self.client = client
        self.client_for: Callable[[str], Any] | None = None  # lane -> the venue that holds it; Kalshi when unset
        self.manual = manual.ManualPositions(client)
        self.store_factory = store_factory
        self.targets: dict[tuple[str, str], Target] = {}  # keyed by (ticker, lane)
        self._paper_rest_at = 0.0
        self._hunts: dict[str, Hunt] = {}  # ticker -> a burst_hunt watch (no position)
        self._hunt_by_series: dict[str, LaneSpec] = {}  # series -> LaneSpec for burst_hunt lanes
        self._lock = threading.Lock()
        self._jobs: queue.SimpleQueue[SellSignal] = queue.SimpleQueue()  # sells for the sell worker. The socket only enqueues
        self._buys: queue.SimpleQueue[BuySignal] = queue.SimpleQueue()  # buys for the buy worker. The socket only enqueues
        self._inflight: set[tuple[str, str]] = set()
        self.follow: Callable[[set[str]], None] | None = None  # start streaming these tickers now (the tape recorder's follow)
        self.tape_factory: Callable[[], Any] | None = None  # opens a read connection to the tape, for the entry context saved with a hand-placed buy
        self._tape: Any = None
        self._fills: queue.SimpleQueue[tuple[float, str, dict[str, Any]]] = queue.SimpleQueue()  # (fill time, ticker, the fill)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.sold: list[str] = []  # log lines, newest last (also logged)

    def _book(self, lane: str) -> Any:
        """The venue for this lane. A Polymarket position is sold there, not on the Kalshi client."""
        if self.client_for is not None:
            return self.client_for(lane)
        return self.client

    # ---- socket side: must be fast and never raise ----
    def on_event(self, e: dict[str, Any]) -> None:
        if e.get("kind") == "fill":
            self._on_fill(e)
            return
        if e.get("kind") == "index" and e.get("index_id") and e.get("value"):
            self._spot[str(e["index_id"])] = (float(e["ts"]), float(e["value"]))
            return
        if e.get("kind") == "trade" and e.get("ticker") and e.get("count"):
            self._on_trade(e)
            return
        if e.get("kind") != "quote" or e.get("bid") is None or e.get("ask") is None:
            return
        self._note_hunt_quote(e)
        with self._lock:
            hits = [t for (ticker, _), t in self.targets.items() if ticker == e["ticker"]]
        for t in hits:
            now = float(e["ts"])
            bid = side_bid(t.side, e["bid"], e["ask"])
            t.cur_bid = bid
            t.cur_other_ask = (1.0 - float(e["bid"])) if t.side == "yes" else float(e["ask"])
            t.history.append((now, bid))
            if bid > 0:
                t.low = min(t.low, bid)
            while t.history and now - t.history[0][0] > HISTORY_KEEP:
                t.history.popleft()
            if t.lane == manual.LANE and t.stop_confirm and not t.stop_ref and bid > 0:
                t.stop_ref = min(t.entry, bid)  # hand-placed: measure the stop from what it could be sold for, not the ask paid (the spread is not a loss)
            self._track_peak(t, bid, now)
            minute_now = (15.0 - (t.close_ts - now) / 60.0) if t.close_ts > 0 else None
            left = (t.close_ts - now) if t.close_ts > 0 else None
            recent = [b for ts, b in t.history if now - 3.0 <= ts <= now and b > 0]
            hovering = len(recent) >= 2 and price_shape(list(t.history), now, bid) == "hover"
            why = take_profit_hit(t.entry, bid, t.x, t.at) or cash_if_up(t.entry, bid, minute_now, t.cash_up, t.cash_drop) or self._scalp(t, bid, now) or cash_if_losing(t.entry, bid, left, t.cash_left, hovering) or early_cash(
                t.entry, bid, t.opened_min, minute_now, before=t.cash_before, leave=t.cash_leave, hovering=hovering,
            ) or spike_hit(
                list(t.history), now, t.entry, t.spike_cents, t.spike_seconds, t.spike_min_gain
            )
            key = (t.ticker, t.lane)
            fast = self._fast_crash(t, bid)
            reason = self._stop_hit(t, bid, now) or fast or why or self._unstable_hit(t, bid, now) or self._hover_hit(t, bid) or self._settlement_hit(t, bid, now, e)
            if reason and t.cross_gap > 0 and e.get("src") == "poly":
                reason = self._cross_checked(t, bid, now, reason)
            if reason and key not in self._inflight:
                self._inflight.add(key)
                self._jobs.put(SellSignal(t.ticker, t.lane, reason))

    def _on_trade(self, e: dict[str, Any]) -> None:
        """A trade on a market we hold. Per second it keeps the contracts traded by YES takers and NO takers over the last two minutes. On the first burst against a held favorite (the last 3 seconds' volume a multiple of the market's own
        normal second, most of it against the holder, and that normal itself at least BURST_MIN_NORMAL) in the last burst_window seconds (the lane's setting) it sells the favorite — that is the peak-crash exit (the bid is still 80c+, volume is dumping, and holding
        those last two minutes lost). A 300-contract print on a silent book is not a crash: the ratio is only meaningful once the market actually trades. It also logs the opposite side as a paper bet (lane burst_flip) and, when burst_buy is set,
        buys a dollar of it. Any 15-minute market, not only crypto. Only for a lane that asked for flips (settlement_flip)."""
        tk, now = e["ticker"], float(e["ts"])
        sec = int(now)
        book = self._flow.setdefault(tk, {})
        row = book.setdefault(sec, [0.0, 0.0, 0.0, 0.0])  # YES-taker contracts, NO-taker contracts, then the dollars they paid (a NO taker pays 1 - the YES price)
        yes_taker = e.get("taker") == "yes"
        row[0 if yes_taker else 1] += float(e["count"])
        row[2 if yes_taker else 3] += float(e["count"]) * (float(e.get("price") or 0.0) if yes_taker else 1.0 - float(e.get("price") or 0.0))
        for old in [k for k in book if k < sec - 125]:
            del book[old]
        with self._lock:
            held = [t for (ticker, _), t in self.targets.items() if ticker == tk and t.settle_flip and (not t.burst_logged or t.burst_resell)]
            hunt = self._hunts.get(tk)
            spec = self._hunt_by_series.get(tk.split("-")[0])
        for t in held:
            idx = 1 if t.side == "yes" else 0  # NO takers against a YES holder, YES takers against a NO holder
            if t.burst_bar > 0:
                # The moonshot's identifier (burst_exit_study, 2026-10-27): at least $X bought AGAINST a 90c favorite in 3 s. Absolute dollars, not a multiple of normal, and a bar per family: the contract-multiple rule sold metals and energy winners early (-2.3c a contract against holding).
                if t.cur_bid < t.burst_favorite or t.cur_other_ask <= 0 or not (t.burst_min_left <= t.close_ts - now <= t.burst_window):
                    continue
                against = sum((book.get(sec - k) or [0.0] * 4)[idx + 2] for k in range(3) if len(book.get(sec - k) or [0.0] * 4) > idx + 2)
                base, ratio, hit = t.burst_bar, against / t.burst_bar, against >= t.burst_bar
                if hit and t.burst_share > 0:
                    with_holder = sum((book.get(sec - k) or [0.0] * 4)[(1 - idx) + 2] for k in range(3) if len(book.get(sec - k) or [0.0] * 4) > (1 - idx) + 2)
                    hit = against / (against + with_holder + 1e-9) >= t.burst_share  # the held side being bought as hard is not a crash
            else:
                if not (BURST_BAND[0] <= t.cur_bid <= BURST_BAND[1]) or t.cur_other_ask <= 0 or not (t.burst_min_left <= t.close_ts - now <= t.burst_window):
                    continue
                against, base, ratio = _side_burst(book, sec, idx)
                hit = _is_side_burst(against, base, ratio)
            if hit:
                first = not t.burst_logged
                t.burst_logged = True
                t.burst_resell = False
                other = "no" if t.side == "yes" else "yes"
                if t.burst_bar > 0:
                    reason = (f"burst: ${against:.0f} bought against the {t.side} holder in 3 s (bar ${t.burst_bar:.0f}), the favorite's bid {t.cur_bid:.2f}; the {other} side costs {t.cur_other_ask:.2f}")
                else:
                    reason = (f"burst: {ratio:.0f}x this side's normal volume against the {t.side} holder ({against:.0f} contracts in 3 s), the favorite's bid {t.cur_bid:.2f}; the {other} side costs {t.cur_other_ask:.2f}")
                ctx = {"ratio": round(ratio, 1), "against_3s": round(against, 1), "normal_second": round(base, 1), "bid": round(t.cur_bid, 4),
                       "other_ask": round(t.cur_other_ask, 4), "left": round(t.close_ts - now, 1), "held_lane": t.lane}
                key = (t.ticker, t.lane)
                if key not in self._inflight:
                    self._inflight.add(key)
                    self._jobs.put(SellSignal(t.ticker, t.lane, reason))  # sell the favorite: the burst is the peak crashing in the last two minutes
                if not first:
                    continue  # a repeat burst after a sale that filled nothing only sells again
                if t.burst_buy > 0 and BUY_BAND[0] <= t.cur_other_ask <= BUY_BAND[1]:
                    self._buys.put(BuySignal("flip", other, round(t.cur_other_ask, 4), reason, ctx, held=t))
                    continue
                try:
                    store = self.store_factory()
                    store.add_decision(ticker=tk, title="", bid=0.0, ask=round(t.cur_other_ask, 4), model_p=0.0, edge=0.0, action="bet", mode="observe", lane="burst_flip", category=_burst_category(tk), side=other, contracts=1.0,
                                       stake_usd=round(t.cur_other_ask, 4), reason=reason, context=json.dumps(ctx), close_time="")
                except Exception:
                    log.debug("burst flip: could not log for %s", tk, exc_info=True)
        if hunt is not None and spec is not None:
            self._on_hunt(hunt, spec, now, book, sec)

    def _note_hunt_quote(self, e: dict[str, Any]) -> None:
        """Remember the YES book on a market a burst_hunt lane named. Close time is filled in on refresh (not on this thread)."""
        spec = self._hunt_by_series.get(str(e["ticker"]).split("-")[0])
        if spec is None:
            return
        with self._lock:
            h = self._hunts.get(e["ticker"])
            if h is None:
                h = Hunt(e["ticker"], spec.name)
                self._hunts[e["ticker"]] = h
            h.yes_bid = float(e["bid"])
            h.yes_ask = float(e["ask"])
            h.quotes.append((float(e["ts"]), h.yes_bid, h.yes_ask))
            while h.quotes and float(e["ts"]) - h.quotes[0][0] > max(spec.hunt_park_seconds, spec.hunt_crash_seconds) + 5.0:
                h.quotes.popleft()

    def _on_hunt(self, h: Hunt, spec: LaneSpec, now: float, book: dict[int, list[float]], sec: int) -> None:
        """No position: buy the cheap side if THAT side's own taker tape just 5x'd. A 95c YES pile-on is ignored; a 5c NO dump is the burst."""
        if h.fired or h.close_ts <= 0 or now < h.retry_at or not (spec.hunt_min_left <= h.close_ts - now <= spec.hunt_window):
            return
        asks = {"yes": h.yes_ask, "no": 1.0 - h.yes_bid}
        lo, hi = spec.price
        for side in spec.sides:
            ask = asks[side]
            if ask <= 0 or ask >= spec.hunt_cheap:
                continue
            if spec.hunt_dollars > 0:
                # The flip trigger (collapse_study / flip_trigger_study, 2026-10-27): a favorite if the lane sets hunt_favorite (it was 90c; 0 = off), and at least $X bought AGAINST it (this cheap side's takers) in the last 3 s, in the last hunt_window seconds. Absolute dollars, not a multiple of normal.
                if not (lo <= ask <= hi):
                    continue
                cheap_mid = (h.yes_bid + h.yes_ask) / 2.0 if side == "yes" else 1.0 - (h.yes_bid + h.yes_ask) / 2.0
                if 1.0 - cheap_mid < spec.hunt_favorite:
                    continue
                bar = spec.hunt_dollars_btc if spec.hunt_dollars_btc > 0 and h.ticker.split("-")[0].startswith("KXBTC") else spec.hunt_dollars
                idx = 0 if side == "yes" else 1
                d3 = sum((book.get(sec - k) or [0.0] * 4)[idx + 2] for k in range(3) if len(book.get(sec - k) or [0.0] * 4) > idx + 2)
                if d3 < bar:
                    continue
                if spec.hunt_spike > 0 and _side_dollar_ratio(book, sec, idx) < spec.hunt_spike:
                    continue  # the bar is met, but not as a surge on this side's own normal buying
                # Dollars on a side that just fell into the band is the favorite winning, not a turn.
                # The buy is a side that was already sitting cheap (the same parked and crash checks as the older hunt).
                crashing, _was = _hunt_crashing(h.quotes, side, now, ask, spec.hunt_crash, spec.hunt_crash_seconds)
                if crashing or not _hunt_parked(h.quotes, side, now, ask, hi, spec.hunt_park_seconds, spec.hunt_park_high, spec.hunt_park_span):
                    continue
                h.fired = True
                reason = f"flip: ${d3:.0f} bought against the {'no' if side == 'yes' else 'yes'} favorite in 3 s (bar ${bar:.0f}), {side} costs {ask:.2f}"
                ctx = {"dollars_3s": round(d3, 1), "bar": bar, "spike": round(_side_dollar_ratio(book, sec, idx), 1), "favorite": round(1.0 - cheap_mid, 3), "ask": round(ask, 4), "left": round(h.close_ts - now, 1), "lane": spec.name}
                self._buys.put(BuySignal("hunt", side, ask, reason, ctx, hunt=h, spec=spec))
                return
            vol, base, ratio = _side_burst(book, sec, 0 if side == "yes" else 1)
            if not (lo <= ask <= hi) or not _hunt_burst(vol, base, ratio):
                continue
            if _side_dollar_ratio(book, sec, 0 if side == "yes" else 1) < BURST_RATIO:
                log.info("burst hunt skipped, %s %s: %.0fx in contracts but under %.0fx in dollars (the price falling, not more money)", h.ticker, side, ratio, BURST_RATIO)
                continue
            crashing, was = _hunt_crashing(h.quotes, side, now, ask, spec.hunt_crash, spec.hunt_crash_seconds)
            if crashing:
                log.info("burst hunt skipped, %s %s is crashing: %.2f, was %.2f within %.0fs", h.ticker, side, ask, was, spec.hunt_crash_seconds)
                continue
            if not _hunt_parked(h.quotes, side, now, ask, hi, spec.hunt_park_seconds, spec.hunt_park_high, spec.hunt_park_span):
                log.info("burst hunt skipped, %s %s has not been sitting under %.0fc", h.ticker, side, max(hi, spec.hunt_park_high) * 100)
                continue
            if not _hunt_repeating(book, sec, 0 if side == "yes" else 1, base):
                log.info("burst hunt skipped, %s %s volume is one print, not repeat buying", h.ticker, side)
                continue
            h.fired = True
            reason = (f"burst hunt: {ratio:.0f}x {side}'s own normal volume ({vol:.0f} contracts in 3 s), {side} costs {ask:.2f}")
            ctx = {"ratio": round(ratio, 1), "with_3s": round(vol, 1), "normal_second": round(base, 1),
                   "ask": round(ask, 4), "left": round(h.close_ts - now, 1), "lane": spec.name}
            self._buys.put(BuySignal("hunt", side, ask, reason, ctx, hunt=h, spec=spec))
            return

    def _run_buys(self) -> None:
        """The buy worker: buys (hunt and flip) come off the queue one at a time, so a slow order never holds up the socket or a sale on the sell worker."""
        while not self._stop.is_set():
            try:
                job = self._buys.get(timeout=1.0)
            except queue.Empty:
                continue
            self._buy_job(job)

    def _drain_buys(self) -> None:
        """Run every queued buy now, on this thread (the worker does the same one at a time)."""
        while True:
            try:
                job = self._buys.get_nowait()
            except queue.Empty:
                return
            self._buy_job(job)

    def _buy_job(self, sig: BuySignal) -> None:
        waited = time.time() - sig.queued
        spec = sig.spec or (lane_specs(self.get_settings()).get(sig.held.lane) if sig.held is not None else None)  # the lane's own limits; a flip buy takes the holding lane's
        stale = spec.hunt_stale_seconds if spec is not None else LaneSpec.hunt_stale_seconds
        if waited > stale:
            log.info("%s buy skipped, %s %s: the buy waited %.0fs in the queue, the price it was read at is gone", sig.kind, sig.ticker, sig.side, waited)
            if sig.hunt is not None:
                sig.hunt.fired = False  # nothing was tried, so the market may fire again at a fresh price (no attempt used)
            return
        sig.ctx["queued_s"] = round(waited, 2)
        if sig.kind == "flip" and sig.held is not None:
            self._burst_buy(sig.held, sig.side, sig.reason, sig.ctx)
        elif sig.hunt is not None and sig.spec is not None:
            h = sig.hunt
            res = self._hunt_buy(h, sig.spec, sig.side, sig.ask, sig.reason, sig.ctx)
            if res is not None and res.outcome == "missed":
                h.attempts += 1
                h.miss_row = res.row_id
                if res.retryable and h.attempts < sig.spec.hunt_attempts:
                    h.retry_at = time.time() + sig.spec.hunt_retry_seconds
                    h.fired = False

    def _hunt_buy(self, h: Hunt, spec: LaneSpec, side: str, ask: float, reason: str, ctx: dict[str, Any]) -> buyer.BuyResult | None:
        """Paper (or live) buy of the cheap side after a hunt burst, under the hunting lane's rules (buyer.hunt_plan). Runs on the buy worker, never on the socket.
        A try after a miss carries the first try's row (h.miss_row), so a fill upgrades that bet instead of adding a second one."""
        try:
            plan = buyer.hunt_plan(spec, h.ticker, side, ask, h.close_ts, reason, ctx, self.client)
            plan.retry_of = h.miss_row
            ctx["attempt"] = h.attempts + 1
            return buyer.buy(plan, self.get_settings(), self.store_factory())
        except Exception:
            log.exception("burst hunt: failed for %s", h.ticker)
            return None

    def _burst_buy(self, t: Target, other: str, reason: str, ctx: dict[str, Any]) -> None:
        """A burst hit a held favorite: buy the other side, a fixed small stake, once per market (buyer.flip_plan). Real when the holding lane is live, a paper bet otherwise or when the order does not fill.
        Runs on the buy worker, never on the socket."""
        try:
            buyer.buy(buyer.flip_plan(t, other, reason, ctx, self._book(t.lane)), self.get_settings(), self.store_factory())
        except Exception:
            log.exception("burst buy: failed for %s", t.ticker)

    def _cross_checked(self, t: Target, bid: float, now: float, reason: str) -> str | None:
        """A sale decided on a Polymarket bid is held back when Kalshi's bid for the same side and market is more than the lane's stop_cross_gap above it: Polymarket prints bids that are not real (exactly 0.15 on markets Kalshi has at 0.75), and a stop sold into one.
        Kalshi's bid is the tape's newest quote for the ticker, at most CROSS_STALE seconds old; with none, the sale goes ahead."""
        from . import tape as tape_mod

        if tape_mod.recent.stop_armed(t.ticker):
            got = tape_mod.recent.stop_quotes(t.ticker, now - CROSS_STALE, now)
            book = None if not got else (float(got[-1][1]), float(got[-1][2]))
        else:
            tape = self._tape_db()
            if tape is None:
                return reason
            try:
                row = tape.execute("SELECT bid, ask FROM quotes WHERE ticker=? AND ts<=? AND ts>? AND bid > 0 AND ask > 0 ORDER BY ts DESC LIMIT 1", (t.ticker, now, now - CROSS_STALE)).fetchone()
            except Exception:
                return reason
            book = None if row is None else (float(row[0]), float(row[1]))
        if book is None:
            return reason
        ref = side_bid(t.side, book[0], book[1])
        if ref - bid <= t.cross_gap:
            return reason
        if now - t.cross_logged > 30.0:
            t.cross_logged = now
            log.warning("watcher: not selling %s %s on a Polymarket bid of %.2f: Kalshi's bid is %.2f (%s)", t.ticker, t.side, bid, ref, reason[:80])
        return None

    def _market_static(self, ticker: str) -> dict[str, Any]:
        """A market read for its fixed fields (close time, strike): kept for ten minutes where the client can, so the 3-second refresh does not ask again."""
        reader = getattr(self.client, "market_static", None)
        return reader(ticker) if reader is not None else self.client.market(ticker)

    def _tape_db(self) -> Any:
        if getattr(self._tls, "tape", None) is None and self.tape_factory is not None:
            self._tls.tape = self.tape_factory()
        return getattr(self._tls, "tape", None)

    def _settlement_hit(self, t: Target, bid: float, now: float, e: dict[str, Any]) -> str | None:
        """The last-minute guard. Kalshi settles a 15-minute crypto market on the 60-second average of the index at the close, so in the last 150 seconds the chance this side finishes ahead can be computed exactly from the spot, the locked part
        of the average and the spot's volatility (settlement.py). When that chance falls `settle_guard` under what the bid pays, holding is worth less than selling, and this says sell. It looks at most once a second, never raises,
        and says nothing when the index ticks or the strike are missing (an unwatched guard is better than a wrong sale)."""
        if t.settle_guard <= 0 or t.strike <= 0 or t.close_ts <= 0 or not t.index_id or bid <= 0:
            return None
        left = t.close_ts - now
        if not (SETTLE_LAST <= left <= SETTLE_WINDOW) or now - t.settle_checked < 1.0:
            return None
        t.settle_checked = now
        p_yes = self._model.chance_yes(t.index_id, t.strike, t.close_ts, now)
        spot = self._spot.get(t.index_id)
        if p_yes is None or spot is None:
            return None
        p_hold = p_yes if t.side == "yes" else 1.0 - p_yes
        if p_hold >= bid - t.settle_guard:
            return None
        ask_other = (1.0 - float(e["bid"])) if t.side == "yes" else float(e["ask"])
        t.flip_hint = (ask_other, 1.0 - p_hold)
        return f"settlement-guard: {left:.0f} s left, the chance this side finishes ahead is {p_hold:.2f} against a bid of {bid:.2f} (spot {spot[1]:.2f} on a target of {t.strike:.2f})"

    def _paper_flip(self, store: Store, row: Any, t: Target, m: dict[str, Any]) -> None:
        """After a settlement-guard sale: log what buying the OTHER side at that moment would have been, as a paper decision in the lane `settlement_flip` (a bet when it costs 5-25c and its chance beats its ask by 5 points, else a pass).
        The market's result settles it like any decision; it is never an order."""
        try:
            ask_o, p_o = t.flip_hint
            if ask_o <= 0:
                return
            other = "no" if (row["side"] or "yes") == "yes" else "yes"
            edge = p_o - ask_o
            bet = 0.05 <= ask_o <= 0.25 and edge >= 0.05
            store.add_decision(ticker=row["ticker"], title=str(m.get("title") or ""), bid=0.0, ask=round(ask_o, 4), model_p=round(p_o, 4), edge=round(edge, 4), action="bet" if bet else "pass", mode="observe",
                               reason=f"settlement flip: {other} costs {ask_o:.2f}, the settlement chance is {p_o:.2f}" + ("" if bet else " (outside 5-25c or under a 5-point edge)"), close_time=str(row["close_time"] or ""), lane="settlement_flip",
                               category="Crypto", event_ticker=str(row["event_ticker"] or ""), side=other, contracts=1.0, stake_usd=round(ask_o, 4))
        except Exception:
            log.debug("settlement flip: could not log for %s", row["ticker"], exc_info=True)

    def _on_fill(self, e: dict[str, Any]) -> None:
        """A buy on the account (from the app or the bot): enroll the market now instead of at the next position poll. Only queues; never raises."""
        s = self.get_settings()
        if not s.manual_exit or e.get("action") not in {"buy", ""} or manual.skipped(e["ticker"], s):
            return
        self._fills.put((float(e["ts"]), e["ticker"], e))

    def enroll_fills(self) -> int:
        """Handle the fills that have waited FILL_SETTLE seconds: stream their markets, then re-read positions and targets once. Returns how many markets."""
        due: set[str] = set()
        events: list[dict[str, Any]] = []
        waiting: list[tuple[float, str, dict[str, Any]]] = []
        while True:
            try:
                ts, ticker, event = self._fills.get_nowait()
            except queue.Empty:
                break
            if time.time() - ts < FILL_SETTLE:
                waiting.append((ts, ticker, event))  # not yet: the bot's own bookkeeping may still be catching up
            else:
                due.add(ticker)
                events.append(event)
        for item in waiting:
            self._fills.put(item)
        if not due:
            return 0
        if self.follow is not None:
            self.follow(due)
        self.manual.invalidate()
        log.info("watcher: fill on %s, enrolled in the socket watch", ", ".join(sorted(due)))
        for event in events:
            try:
                self._log_manual_entry(event)
            except Exception:
                log.exception("watcher: could not log a hand-placed buy")
        self.refresh()
        return len(due)

    def _log_manual_entry(self, e: dict[str, Any]) -> None:
        """A buy the bot did not place: save the market's entry context at the fill (factors.manual_report) and a decisions row so digest can score it."""
        from . import factors

        store = self.store_factory()
        if not e.get("trade_id") or store.is_bot_order(e.get("order_id", "")):
            return
        side, price = e["side"], e.get("price")
        paid = None if price is None else (price if side == "yes" else 1.0 - price)
        close_ts, close_time, snap = None, "", ""
        try:
            close_time = str(self._market_static(e["ticker"]).get("close_time", ""))
            close_ts = datetime.fromisoformat(close_time).timestamp()
            if self.tape_factory is not None:
                if self._tape is None:
                    self._tape = self.tape_factory()
                got = factors.snapshot(self._tape, e["ticker"], side, close_ts, float(e["ts"]))
                snap = json.dumps(got) if got else ""
        except (KalshiError, TypeError, ValueError):
            close_time = ""
        if store.db.execute("SELECT 1 FROM manual_entries WHERE trade_id=?", (e["trade_id"],)).fetchone():
            return
        store.add_manual_entry(trade_id=e["trade_id"], ts=float(e["ts"]), ticker=e["ticker"], side=side, price=paid, count=e.get("count"),
                               order_id=e.get("order_id"), close_ts=close_ts, factors=snap)
        self._note_manual_study(store, ticker=e["ticker"], side=side, paid=paid, count=float(e.get("count") or 0) or 1.0,
                                close_time=close_time if close_ts else "", replace=False)
        log.info("watcher: logged your buy of %s %s at %s with its entry context", e["ticker"], side, "?" if paid is None else f"{paid:.2f}")

    def _note_manual_study(self, store: Store, *, ticker: str, side: str, paid: float | None, count: float, close_time: str, replace: bool) -> None:
        """A decisions row so digest/review can score the hand-placed buy after it settles. Observe-mode, excluded from paper bankroll."""
        if paid is None or paid <= 0 or count <= 0:
            return
        s = self.get_settings()
        stake = round(count * (paid + fee_per_contract(paid)), 4)
        row = store.db.execute(
            """SELECT id, contracts, stake_usd FROM decisions
               WHERE ticker=? AND COALESCE(lane,'')=? AND action='bet' AND outcome IS NULL AND exit_ts IS NULL
               ORDER BY id LIMIT 1""",
            (ticker, manual.LANE),
        ).fetchone()
        if row is not None:
            if replace:
                store.db.execute(
                    "UPDATE decisions SET contracts=?, stake_usd=?, ask=? WHERE id=?",
                    (count, stake, round(paid, 4), row["id"]),
                )
                store.db.commit()
            else:
                store.db.execute(
                    "UPDATE decisions SET contracts=?, stake_usd=? WHERE id=?",
                    ((row["contracts"] or 0.0) + count, (row["stake_usd"] or 0.0) + stake, row["id"]),
                )
                store.db.commit()
            return
        store.add_decision(
            ticker=ticker, title="", bid=0.0, ask=round(paid, 4), model_p=0.0, edge=0.0,
            action="bet", reason="hand-placed moonshot" if paid <= s.manual_cheap_max else "hand-placed buy",
            mode="observe", close_time=close_time or "",
            lane=manual.LANE, category=_burst_category(ticker), side=side,
            contracts=count, stake_usd=stake,
        )

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

    def _scalp(self, t: Target, bid: float, now: float) -> str | None:
        """Armed at scalp_at, or scalp_up above the price paid, whichever is reached first. A new high rides; a dip of scalp_dip from that high sells once the bid has stayed that far under it for
        scalp_dip_seconds. A bid back inside the dip, or a new high, starts the count again. The clock loop calls this too, so a quiet market (no quotes) still sells a dip that has held."""
        arms = [p for p in (t.scalp_at, t.entry + t.scalp_up if t.scalp_up > 0 else 0.0) if p > 0]
        arm = min(arms) if arms else 0.0
        if arm <= 0 or t.scalp_dip <= 0 or bid <= 0:
            return None
        if bid + 1e-9 >= arm:
            t.scalp_high = max(t.scalp_high, bid)
        hit = scalp_hit(bid, t.scalp_high, arm, t.scalp_dip)
        if hit is None:
            t.scalp_dip_since = None
            return None
        if t.scalp_dip_seconds <= 0:
            return hit
        if t.scalp_dip_hard > 0 and t.scalp_high - bid >= t.scalp_dip_hard - 1e-9:
            return f"{hit} (hard drop)"  # a sharp reversal does not wait: the bid keeps falling while the count runs
        if t.scalp_dip_since is None:
            t.scalp_dip_since = now
        held = now - t.scalp_dip_since
        return f"{hit} for {held:.0f}s" if held >= t.scalp_dip_seconds else None

    def _stop_hit(self, t: Target, bid: float, now: float) -> str | None:
        """The stop-loss, on the socket: the bid is `stop_drop` under the price paid (and, for an hourly market, the last hour has begun)."""
        if t.stop_drop <= 0 or bid <= 0 or t.entry <= 0:
            return None
        if time.time() < t.retry_after:
            return None
        ref = t.stop_ref or t.entry
        left = t.close_ts - now if t.close_ts else None
        open_market = left is not None and left > 0
        if open_market and t.watch_loss > 0 and ref > 0 and bid <= ref * (1.0 - t.watch_loss):
            if t.loss_since is None:
                t.loss_since = now
                t.loss_low = bid
            elif bid < t.loss_low:
                t.loss_low = bid
            hit = hard_loss(
                ref, bid, now - t.loss_since, t.watch_loss, t.watch_loss_seconds, low=t.loss_low, lift=t.watch_loss_lift,
            )
            return f"{hit} (socket)" if hit else None
        t.loss_since = None
        t.loss_low = 0.0
        if open_market and t.watch_at > 0 and ref > t.watch_at and bid <= t.watch_at:
            if t.stop_since is None:
                t.stop_since = now
            trend = price_shape(list(t.history), now, bid) == "trend"
            hit = watch_trend(
                ref, bid, left, now - t.stop_since, line=t.watch_at,
                ride=t.watch_ride, patient_left=t.watch_patient, trend=trend,
            )
            return f"{hit} (socket)" if hit else None  # this visit is being watched: a hover or a spike is not the 45% line's sale
        if t.watch_at > 0 and bid > t.watch_at:
            t.stop_since = None
        if now < t.stop_from:
            return None
        if bid > ref * (1 - t.stop_drop) + 1e-9:
            t.stop_since = None
            return None
        if t.stop_since is None:
            t.stop_since = now
        # Last 2 minutes: the loss is under 50c, and the last 3 seconds say which kind. A trend sells
        # on this print. A spike or a hover does not, and there is no extra wait.
        if (
            t.close_ts and t.ticker.split("-")[0].endswith("15M") and 0 < t.close_ts - now <= 120
            and (bid >= 0.50 or price_shape(list(t.history), now, bid) != "trend")
        ):
            t.stop_since = None
            return None
        # How long this dip must last depends on the clock (strategy.stop_wait): none inside 2 minutes,
        # none past 7 on a 15-minute market, up to the lane's cap at 2-5 minutes, where dips rebound.
        wait = stop_wait(t.stop_confirm, t.close_ts - now if t.close_ts else None, t.ticker.split("-")[0].endswith("15M"))
        if now - t.stop_since < wait:
            return None  # a dip is not a loss until it lasts: wait for the price to turn
        held = f" for {now - t.stop_since:.0f}s" if wait else ""
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
            key = (t.ticker, t.lane)
            if t.scalp_dip_since is not None and t.last_ts and key not in self._inflight:
                why = self._scalp(t, t.last_bid, now)  # a dip that has held 3 s while no quote arrived
                if why:
                    self._inflight.add(key)
                    self._jobs.put(SellSignal(t.ticker, t.lane, why))
                    continue
            if not t.last_ts or (t.trail_drop <= 0 and t.stop_drop <= 0):
                continue
            if now - t.bid_since >= t.trail_hold:
                t.peak = max(t.peak, t.last_bid)
            t.below_since = self._since(t, t.last_bid, now)
            key = (t.ticker, t.lane)
            fast = self._stop_hit(t, t.last_bid, now) or self._fast_crash(t, t.last_bid)
            if fast and key not in self._inflight:
                self._inflight.add(key)
                self._jobs.put(SellSignal(t.ticker, t.lane, fast))
                continue
            if t.below_since is not None and now - t.below_since >= t.trail_confirm and key not in self._inflight and now >= t.retry_after:
                why = None if below_floor(t, t.last_bid) else trail_hit(t.entry, t.peak, t.last_bid, arm_gain(t), crash_drop(t))
                if why:
                    self._inflight.add(key)
                    self._jobs.put(SellSignal(t.ticker, t.lane, f"{why} for {now - t.below_since:.0f}s"))

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
            guard = spec.settlement_guard > 0 and row["ticker"].split("-")[0].endswith("15M") and index_for_series(row["ticker"].split("-")[0]) is not None
            ticker, lane = row["ticker"], row["lane"] or "moonshot"
            with self._lock:
                old = self.targets.get((ticker, lane))
            fresh[(ticker, lane)] = old or Target(
                ticker, lane, row["side"] or "yes", float(row["ask"] or 0.0), spec.take_profit_x, spec.take_profit_at,
                spec.spike_cents, spec.spike_seconds, spec.spike_min_gain, spec.trail_activate, spec.trail_drop, spec.trail_confirm_seconds, spec.trail_fast_drop, spec.trail_hold_seconds,
                float(row["ask"] or 0.0),
            )
            fresh[(ticker, lane)].unstable_gain = spec.unstable_gain
            if fresh[(ticker, lane)].close_ts <= 0 and row["close_time"]:
                with contextlib.suppress(TypeError, ValueError):
                    fresh[(ticker, lane)].close_ts = datetime.fromisoformat(row["close_time"]).timestamp()
            flip = spec.settlement_flip and ticker.split("-")[0].endswith("15M")  # volume bursts are watched on every 15-minute market, not only crypto
            if guard or flip:
                tg = fresh[(ticker, lane)]
                tg.settle_guard, tg.settle_flip = (spec.settlement_guard if guard else 0.0), spec.settlement_flip and flip
                tg.burst_buy = spec.burst_buy if tg.settle_flip else 0.0
                btc = ticker.split("-")[0].startswith("KXBTC")
                tg.burst_bar = 0.0 if (btc and spec.burst_dollars_btc < 0) else (spec.burst_dollars_btc if (btc and spec.burst_dollars_btc > 0) else spec.burst_dollars)
                tg.burst_favorite, tg.burst_window, tg.burst_min_left, tg.burst_share = spec.burst_favorite, spec.burst_window, spec.burst_min_left, spec.burst_share
                tg.burst_spot_edge = spec.burst_spot_edge
                tg.index_id = (index_for_series(ticker.split("-")[0]) or "") if guard else ""
                if tg.close_ts <= 0 or (guard and tg.strike <= 0):
                    try:
                        mk = self._market_static(ticker)
                        tg.strike = float(mk.get("floor_strike") or 0.0)
                        tg.close_ts = datetime.fromisoformat(str(mk.get("close_time"))).timestamp()
                    except (TypeError, ValueError, KalshiError):
                        tg.settle_guard = 0.0  # no strike or close time: better unguarded than guarded on a guess
                        tg.settle_flip = False
            fresh[(ticker, lane)].unstable_range = spec.unstable_range
            fresh[(ticker, lane)].unstable_seconds = spec.unstable_seconds
            fresh[(ticker, lane)].hover_dip = spec.hover_dip
            fresh[(ticker, lane)].trail_progress = spec.trail_progress if spec.trail_progress is not None else s.crash_progress
            fresh[(ticker, lane)].trail_giveback = spec.trail_giveback if spec.trail_giveback is not None else s.crash_giveback
            fresh[(ticker, lane)].spot_sigma = spec.trail_spot_sigma if spec.trail_spot_sigma is not None else s.crash_spot_sigma
            held = fresh[(ticker, lane)]
            held.cash_before, held.cash_leave, held.cash_at = spec.cash_before_minute, spec.cash_leave_minute, spec.cash_at
            held.cash_up, held.cash_drop, held.cash_left = spec.cash_up_minute, spec.cash_drop, spec.cash_left_seconds
            held.watch_at = spec.watch_at
            held.watch_ride, held.watch_patient = spec.watch_ride_seconds, spec.watch_patient_left
            held.watch_loss, held.watch_loss_seconds, held.watch_loss_lift = spec.watch_loss, spec.watch_loss_seconds, spec.watch_loss_lift
            held.cross_gap = spec.stop_cross_gap
            held.scalp_at, held.scalp_up, held.scalp_dip, held.scalp_dip_seconds = spec.scalp_at, spec.scalp_up, spec.scalp_dip, spec.scalp_dip_seconds
            held.scalp_dip_hard = spec.scalp_dip_hard
            if row["close_time"] and str(row["ticker"]).split("-")[0].endswith("15M"):
                with contextlib.suppress(TypeError, ValueError):
                    close = datetime.fromisoformat(row["close_time"]).timestamp()
                    held.opened_min = 15.0 - (close - float(row["ts"])) / 60.0
                    if held.close_ts <= 0:
                        held.close_ts = close
            if stop_on:
                fresh[(ticker, lane)].stop_drop = stop_drop_for(spec, s)
                fresh[(ticker, lane)].stop_confirm = spec.stop_confirm_seconds
                try:
                    close = datetime.fromisoformat(row["close_time"]).timestamp()
                    fresh[(ticker, lane)].stop_from = close - (spec.stop_from_seconds if spec.stop_from_seconds > 0 else s.stop_loss_hours * 3600)  # a lane may keep its stop quiet until this many seconds remain
                    if fresh[(ticker, lane)].close_ts <= 0:
                        fresh[(ticker, lane)].close_ts = close  # the stop's wait is calculated from the time left; the settlement guard's own close (from the market) wins when it set one
                except (TypeError, ValueError):
                    fresh[(ticker, lane)].stop_from = 0.0
        if s.manual_exit and getattr(self.client, "authed", False):
            self._add_manual(s, store, fresh)
        for ticker, _lane in fresh:
            runner.arm_stop_history(s, ticker)  # one read of the last 15 minutes; live quotes extend it
        for held in fresh.values():
            if held.index_id:
                runner.arm_index_history(s, held.index_id)
        hunt_by = {ser: spec for spec in enabled_lanes(s) if spec.burst_hunt for ser in spec.series}
        with self._lock:
            self.targets = fresh
            self._hunt_by_series = hunt_by
            for tk in [tk for tk, h in self._hunts.items() if tk.split("-")[0] not in hunt_by]:
                del self._hunts[tk]
            hunts = list(self._hunts.values())
        now = time.time()
        for tk in [tk for tk, b in list(self._flow.items()) if max(list(b), default=0) < now - 300.0]:
            self._flow.pop(tk, None)  # a market that stopped trading five minutes ago: the ledger only trims itself when a trade arrives, so a closed window's rows would stay for good
        follow: set[str] = set()
        for h in hunts:
            if h.close_ts <= 0:
                try:
                    mk = self._market_static(h.ticker)
                    h.close_ts = datetime.fromisoformat(str(mk.get("close_time"))).timestamp()
                except (TypeError, ValueError, KalshiError):
                    h.close_ts = 0.0
            if h.close_ts and now - 7 * 86400 < h.close_ts < now - 5:
                with self._lock:
                    self._hunts.pop(h.ticker, None)
                continue
            hs = hunt_by.get(h.ticker.split("-")[0])
            if h.close_ts and 0 < h.close_ts - now <= (hs.hunt_window if hs is not None else LaneSpec.hunt_window) + 30:
                follow.add(h.ticker)
        if follow and self.follow is not None:
            self.follow(follow)
        self._book_resting(store, s)
        if time.time() - self._paper_rest_at >= 10.0:
            self._paper_rest_at = time.time()
            try:
                got = paperfill.settle(s, store, self._tape_db())
                if got["filled"] or got["missed"]:
                    log.info("watcher: paper resting buys judged: %d would have filled, %d would not", got["filled"], got["missed"])
            except Exception:
                log.debug("watcher: paper resting judgement failed", exc_info=True)

    def _book_resting(self, store: Store, s: Settings) -> None:
        """Book fills of resting take-profit sells (runner._rest_take_profit) and close the bets they finish.

        A fill can land at any moment, long after the order was placed; this is what notices it. Runs on the
        refresh cycle, so a fill is on the books within a few seconds. A partial fill is booked and the rest
        of the order keeps working; nothing here sells anything itself.
        """
        for row in store.open_bets():
            lane = row["lane"] or "moonshot"
            if not s.is_live(lane, row["ticker"].split("-")[0]) or store.working_rest_sell(row["ticker"], s.env) is None:
                continue
            try:
                new, total, avg = runner._book_resting_fills(s, self.client, store, row["ticker"])
            except Exception:
                log.debug("watcher: could not book the resting sell on %s", row["ticker"], exc_info=True)
                continue
            if new > 0 and total > 0 and store.net_contracts(row["ticker"], s.env) < 0.005:
                store.mark_exited(row["ticker"], avg, f"resting take-profit filled at {avg:.4f} (no taker fee)", lane)
                line = f"sold {total:g} {row['ticker']} resting @ {avg:.4f} (no taker fee)"
                log.info("watcher: %s", line)
                self.sold.append(line)

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
            if not store.holds_sized_bet(p.ticker, manual.LANE):
                close_time = ""
                with contextlib.suppress(KalshiError, TypeError, ValueError):
                    close_time = str(self._market_static(p.ticker).get("close_time", ""))
                self._note_manual_study(store, ticker=p.ticker, side=p.side, paid=p.entry, count=p.count, close_time=close_time, replace=True)
            fresh[key] = old

    @staticmethod
    def _cheap_rules(s: Settings, t: Target) -> None:
        """A hand-placed buy at or under MANUAL_CHEAP_MAX is a moonshot: hold to settlement. No take-profit, no trail, no stop (a 2¢
        ticket otherwise sells on a 4¢ bounce). Optional MANUAL_CHEAP_TAKE_X / ARM_X / GIVEBACK still cash it out if you set them.
        Dearer positions keep the general hand-placed rules. Applied on every refresh, so adding to a position follows the new average."""
        if 0 < t.entry <= s.manual_cheap_max:
            t.x = s.manual_cheap_take_x if s.manual_cheap_take_x > 0 else 0.0
            t.trail_progress = 0.0
            t.stop_drop, t.stop_since = 0.0, None
            if s.manual_cheap_arm_x > 1.0 and s.manual_cheap_giveback > 0:
                t.trail_activate = s.manual_cheap_arm_x - 1.0
                t.trail_giveback = s.manual_cheap_giveback
            else:
                t.trail_activate = t.trail_drop = t.trail_fast = t.trail_giveback = 0.0
        else:
            t.x = 0.0
            t.trail_activate = s.manual_exit_activate

    def _arm_stop(self, s: Settings, t: Target) -> None:
        """The bot's own stop-loss rules for a hand-placed position: a 15-minute market only when stop_loss_fast is on, any other
        market only in its last STOP_LOSS_HOURS."""
        if s.manual_exit_stop <= 0 or (t.ticker.split("-")[0].endswith("15M") and not s.stop_loss_fast):
            return
        try:
            close = datetime.fromisoformat(str(self._market_static(t.ticker).get("close_time", ""))).timestamp()
        except (TypeError, ValueError, KalshiError):
            return  # no close time, no stop: better unwatched than stopped on a guess
        t.stop_drop = s.manual_exit_stop
        t.stop_confirm = s.manual_exit_stop_confirm
        t.stop_from = close - s.stop_loss_hours * 3600
        # close_ts deliberately untouched: a hand-placed position keeps the flat confirm its owner set
        # (MANUAL_EXIT_STOP_CONFIRM); the calculated wait is for the bot's own lanes.

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
        key = (ticker, lane)
        if held is None or (entry > 0.5 and held <= 0.5) or held <= bid - fee_per_contract(bid) + HOLD_MARGIN:
            self._soft_holds.pop(key, None)
            return False
        # The first "hold" is soft: it only buys a short wait, after which the stop fires again and the model is asked again with the price then. Only a second
        # "hold" in a row (asked at least the recheck wait later) is firm and lets the stop rest for the usual back-off.
        now, recheck = time.time(), s.stop_model_recheck_seconds
        first = self._soft_holds.get(key)
        if first is None or now - first > 4 * recheck:
            self._soft_holds[key] = now
            log.info("watcher: stop on %s softly held: the model values it at %.2f against a bid of %.2f; asking again in %.0fs", ticker, held, bid, recheck)
            self._back_off(ticker, lane, recheck)
        else:
            self._soft_holds[key] = now
            log.info("watcher: stop on %s held: the model still values it at %.2f against a bid of %.2f", ticker, held, bid)
            self._back_off(ticker, lane)
        return True

    def _back_off(self, ticker: str, lane: str, seconds: float = RETRY_AFTER) -> None:
        """A declined sale is not re-checked for RETRY_AFTER seconds, so a position sitting under its price costs no API calls."""
        with self._lock:
            t = self.targets.get((ticker, lane))
        if t is not None:
            t.retry_after = time.time() + seconds

    def _burst_spot_holds(self, t: Target, ticker: str, side: str, bid: float) -> bool:
        """A burst sells on order flow alone, and a one-second wick in the reference price looks the same as a crash. When spot's own chance that this side pays is more than `burst_spot_edge` over what the sale gets (the bid less its fee), the
        sale is worth less than holding, so wait: the next burst may sell again, and the stop-loss still protects the downside. Unknown spot means no opinion: the sale goes ahead."""
        if t.burst_spot_edge <= 0 or self.spot_side is None or bid <= 0:
            return False
        try:
            p = self.spot_side(ticker, side)
        except Exception as exc:
            log.debug("watcher: no spot opinion for %s (%s)", ticker, exc)
            return False
        sale = bid - fee_per_contract(bid)
        if p is None or p <= sale + t.burst_spot_edge:
            return False
        log.info("watcher: holding %s through the burst: spot gives this side a %.0f%% chance against a sale at %.2f, so the dip is a wick", ticker, 100 * p, sale)
        t.burst_resell = True
        self._back_off(ticker, t.lane)
        return True

    def _spot_holds(self, t: Target | None, ticker: str, side: str, why: str, bid: float = 0.0) -> bool:
        """A crash exit protects a profit from a collapse. While spot is still comfortably on the winning side of the strike (more than
        `spot_sigma` sigma) a bid that dipped is noise, so wait and look again. Unknown spot means no opinion: the exit goes ahead."""
        if t is not None and why.startswith("burst"):
            return self._burst_spot_holds(t, ticker, side, bid)
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
            if why.startswith("take-profit") and store.working_rest_sell(ticker, s.env) is not None:
                return  # a resting sell is working this target (runner._rest_take_profit): it fills with no taker fee, or its own life ends at the close and this rule takes over again; crossing the book here would only pay the fee it exists to avoid
            book = self._book(lane)
            m = book.market(ticker)
            if m.get("result") in {"yes", "no"}:
                return
            side = row["side"] or "yes"
            quoted = flip(m) if side == "no" else m
            bid = 0.0
            with contextlib.suppress(TypeError, ValueError):
                bid = float(quoted.get("yes_bid_dollars") or 0.0)
            entry = float(row["ask"] or 0.0)
            resting_paper = (spec := lane_specs(s).get(lane)) is not None and spec.rest_sell and not s.is_live(lane, ticker.split("-")[0])
            cut = why.startswith(("peak-crash", "stop-loss", "settlement-guard", "burst")) or (why.startswith("cash-out:") and " under the " in why)
            if bid - fee_per_contract(bid) <= entry + fee_per_contract(entry) and not resting_paper and not cut:  # a profit-take never sells for a loss after both fees; a peak-crash, a stop, the settlement guard, a last-two-minutes burst, and a timed drop do (the point is to get out). A paper lane that rests its sale is not re-read at all: its order filled at the target when the bid touched it, so a dipped re-read cannot veto it
                self._back_off(ticker, lane)
                return
            if bid <= 0:
                return
            with self._lock:
                tgt = self.targets.get((ticker, lane))
            if why.startswith("peak-crash") and tgt is not None and below_floor(tgt, bid):
                self._back_off(ticker, lane)  # under the price paid the stop-loss decides, not the crash exit
                return
            if self._spot_holds(tgt, ticker, side, why, bid):
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
            line = runner.exit_position(s, book, store, row, bid, why + " [socket]", slip=slip)
            log.info("watcher: %s", line)
            self.sold.append(line)
            if why.startswith("burst") and t is not None and line.startswith("partial exit"):
                t.burst_resell = True  # the burst sale did not finish: the next burst tries again
            if why.startswith("settlement-guard") and t is not None and t.settle_flip and line.startswith(("sold", "paper-sold")):
                self._paper_flip(store, row, t, m)
        except KalshiError as exc:
            log.warning("watcher: could not sell %s (%s)", ticker, exc)
        finally:
            self._inflight.discard((ticker, lane))

    def _sell_manual(self, s: Settings, store: Store, ticker: str, why: str) -> None:
        """Sell a hand-placed position, only for a profit, and only when the account really holds it and trading is on."""
        pos = next((p for p in self.manual.positions(s, store, fresh=True) if p.ticker == ticker), None)
        if pos is None:
            return  # sold, settled, or now the bot's
        m = self.client.market(ticker)  # a live bid and the result: never the kept copy
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
        floor = (pos.entry + fee_per_contract(pos.entry)) * (1 + s.manual_exit_min_profit) + fee_per_contract(bid)  # the cost includes the entry fee; the sale pays its own
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
        if filled >= pos.count - 0.005:
            store.mark_exited(ticker, avg, why, manual.LANE)
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
            (self._run_refresh, "watch-refresh"), (self._run_sells, "watch-sell"), (self._run_buys, "watch-buy"), (self._run_timers, "watch-timer"),
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

