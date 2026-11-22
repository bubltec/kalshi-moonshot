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
import os
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, NamedTuple

from . import (
    checkins,
    curverisk,
    curvevalue,
    manual,
    manualrest,
    memguard,
    paperfill,
    pathshape,
    perpref,
    pricevol,
    quoteshock,
    runner,
    sellchain,
    settlement,
    steptime,
    variants,
)
from . import ticks as ticks_mod
from .biquotefeed import index_for_series as biquote_index
from .config import Settings
from .kalshi import KalshiClient, KalshiError
from .kalshiws import index_for_series
from .lanes import LaneSpec, lane_specs
from .store import Store
from .strategy import (
    fee_per_contract,
    flip,
    price_shape,
    stop_wait,
    take_profit_hit,
    trail_hit,
)

log = logging.getLogger("moonshot")

SETTLE_WINDOW = 150.0  # the settlement guard looks at a 15-minute crypto bet in its last 150 seconds. Widened to 420 s on 2026-10-25 from a replay, but live it sold 13 positions at 200-345 s and 12 went on to win (-0.34 vs +1.55 per contract held): back to 150
BURST_RATIO = 5.0  # this SIDE's last 3 seconds of taker buys vs this side's own median second (the 2 minutes before). A pile-on at 95c is not a 5c burst
BURST_FLOOR = 300.0  # a burst must also be at least this many contracts on that side, so a handful of cheap prints never counts
BURST_MIN_NORMAL = 10.0  # 5x of a silent side is not a burst. That side must trade this many contracts in a typical second
BURST_BAND = (0.80, 0.97)  # the held side's bid must be in this band: a favorite
SHOCK_PERSIST_GAP = 15.0  # seconds without a reading that break a shock_persist run
SETTLE_LAST = 3.0  # ... and not in the last 3 (an order would land after the close)
THROUGH_QUOTE = 1.0  # a bid older than this, before the index tick that went through, is the quote from before the cross
THROUGH_SPOT = 5.0  # an index tick older than this is not a reason to sell
SHOCK_BID = (0.05, 0.89)  # the shock exit sells only a bid in this range: below it the market has already priced the loss, above it the market is not worried
SHOCK_LOG_MIN = 3.0  # bp of move against a held bet that is worth a shock_log row

CURVE_GO_MIN_LEFT = 8.0  # the time-to-go sale keeps the same last-seconds guard (an order would land after the close)
CURVE_MIN_LEFT = 8.0  # the curve exit does not act in the last 8 s (an order would land after the close)
SHOCK_KEEP = 130.0  # seconds of index ticks kept for the shock exit to look back over
THROUGH_STAY = 45.0  # outside the last minute, this long continuously through is a sale. A shorter dip is the one that comes back and pays
LEDGER_EVERY = 300.0  # seconds between refreshes of the favorites ledger (variants.refresh_ledger)
REFRESH_EVERY = 3.0  # seconds between reloads of the open-bet list
BURST_BID_SLACK = 0.15  # a burst sale is held back when the bid read just before the order is more than this under the favorite's bid that triggered it: a thin book flickers (copper, 2026-11-02: 0.91 at the trigger, 0.19 a second later, and the bet settled a winner)
HOLD_MARGIN = 0.01  # holding must beat the sale value by this much before an exit is withheld
RETRY_AFTER = 15.0  # seconds before a declined sale is looked at again
SOCKET_QUIET = 20.0  # seconds without a quote (or any stream event) before the sweep decides a position's loss exit instead of the socket
CROSS_STALE = 15.0  # a Kalshi quote older than this is not compared with a Polymarket bid
HISTORY_KEEP = 120.0  # seconds of bids kept per market, for the spike rule
FILL_SETTLE = 3.0  # seconds after a fill before positions are read: the bot's own bet must be on the books first, or it would look hand-placed



class SellSignal(NamedTuple):
    """A watcher's call to sell a held position; the sell worker acts on it. A plain 3-tuple underneath, so it unpacks as (ticker, lane, why)."""
    ticker: str
    lane: str
    why: str


def perp_index(series: str) -> str | None:
    """The Kalshi perp reference tape index that follows a 15-minute series, commodities and coins alike (perpref.index_for_series has no coins: their settlement index is Kalshi's own, but the perp is the long-horizon layer, 2026-11-13, user: "use Kalshi perp as our additional layer for long term analysis")."""
    ix = perpref.index_for_series(series)
    if ix:
        return ix
    base = series.removeprefix("KX").removesuffix("15M")
    ix = f"KP_{base}"
    return ix if ix in perpref.FEEDS and series.endswith("15M") else None


class _SellWalk:
    """What the sale's steps share on one risk check (ProfitWatcher._risk_decide): the readings every step may use, and what a step computed for the steps after it. Made new on every check."""

    __slots__ = ("bar", "basis", "early", "gain", "long_chance", "look", "net", "rebuy", "room", "shared", "take_state", "taken", "unit")

    def __init__(self, look: float, basis: float, net: float, long_chance: float | None) -> None:
        self.look, self.basis, self.net, self.long_chance = look, basis, net, long_chance
        self.shared = False  # the slope history (_sell_shared)
        self.take_state, self.gain, self.room, self.rebuy, self.unit, self.bar = False, 0.0, 0.0, 0.0, None, 0.0  # the gain's readings (_sell_take_state)
        self.taken, self.early = False, False  # what the profit take decided on this check, for the trail


@dataclass
class Target:
    ticker: str
    lane: str
    side: str
    entry: float  # price paid, in the side's own terms
    x: float = 0.0  # a hand-placed cheap buy's take level (manual_cheap_take_x); lanes set none
    at: float = 0.0
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
    rest_rule: bool = False  # a hand-placed position the resting curve sell is working (manualrest.py)
    retry_after: float = 0.0  # after a sale was checked and declined, do not look again before this time
    sell_misses: int = 0  # sales in a row that filled nothing: each one doubles the wait before the next try
    stop_drop: float = 0.0  # stop-loss: sell at once when the bid is this fraction under the price paid (0 = this position has none)
    unstable_gain: float = 0.0  # sell when up this fraction after fees and the bid is swinging (0 = off)
    unstable_range: float = 0.0
    unstable_seconds: float = 20.0
    low: float = 1.0  # lowest bid seen since this position was opened
    stop_confirm: float = 0.0  # the bid must stay past the stop line this many seconds before the stop sells (0 = at once)
    stop_since: float | None = None  # when the bid first fell past the stop line and has stayed there
    stop_ref: float = 0.0  # price the stop is measured from when it is not the price paid (a hand-placed buy: the bid it could be sold at)
    stop_from: float = 0.0  # ... but only once this unix time is reached (an hourly market is only stopped in its last STOP_LOSS_HOURS)
    trail_progress: float = 0.0  # >0: the crash exit arms once the bid has covered this share of the way from the price paid to $1.00, instead of trail_activate
    spot_sigma: float = 0.0  # the crash exit holds while spot is more than this many sigma on the winning side of the strike (0 = off)
    trail_giveback: float = 0.0  # >0: the crash exit sells once this share of the gain (peak over price paid) is given back, instead of trail_drop (0 = the fixed drop)
    strike: float = 0.0  # the market's target
    close_ts: float = 0.0
    opened_min: float = 1e9  # minutes into the window when this buy was placed
    entry_filled: bool = False  # entry is the price the buy orders actually filled at (store.entry_fill), not the decision's ask
    curve_scales: str = "900+180"
    risk_live: bool = False
    sell_chain: tuple = ()  # the sale's steps in the lane's own order (lane field sell_chain, or its exit module's steps; sellchain.py); empty = the default order, and the sale reasons as they always were
    sell_module: str = ""  # the lane's exit module, named in a chain lane's sale note
    bought_ts: float = 0.0  # when the bet was bought (its decision's time): the start of the shape step's since-the-buy look-back
    shape_path: deque = field(default_factory=lambda: deque(maxlen=1200))  # (time, our side's bid) on every risk check since the bet was first watched: the path the shape step reads (sell step `shape`, pathshape.py); kept only on a lane whose chain names the step
    shape_at: float = 0.0  # when the shape was last read (once every pathshape.STEP seconds)
    shape_pairs: set = field(default_factory=set)  # the (since-the-buy shape, quarter shape) pairs already written for this bet
    shape_would: bool = False  # the one "would sell" log line has been written
    shape_failed: bool = False  # the shape step raised on this bet and the one warning has been written
    timed_since: float = 0.0  # shape_timed: when the bet last went behind and has stayed behind (0 = not behind)
    timed_at: float = 0.0  # shape_timed: when the shape was last read (once every pathshape.STEP seconds)
    timed_done: bool = False  # shape_timed: its one row has been written for this bet
    timed_failed: bool = False  # the shape_timed step raised on this bet and the one warning has been written
    risk_spot_weight: float = 0.5
    risk_history: bool = True
    risk_min_profit: float = 0.01
    risk_unit: float | None = None
    risk_unit_at: float = 0.0
    risk_unit_timed: float | None = None  # the time-aware typical minute (pricevol.implied_rate), read at most every 5 s (sell step trail_timed, shadow)
    risk_unit_timed_at: float = 0.0
    trail_timed_last: tuple = (False, False)  # the (static, time-aware) trail verdicts last written for this bet
    trail_timed_failed: bool = False  # the trail_timed step raised on this bet and the one warning has been written
    risk_take: bool = False  # the profit take (lane field curve_take)
    risk_take_cushion: bool = False  # the spot's cushion is asked before the take sells (lane field curve_take_cushion)
    risk_take_held: bool = False  # the cushion has already been logged as holding this bet's take
    risk_take_slope: float = 0.08  # the look back for the slope as a share of the time left: no profit sale while the bid is still sloping up over it (lane field curve_take_slope_share)
    risk_take_sure: float = -1.0  # the profit take holds at a bid at or over this (lane field curve_take_sure)
    risk_take_sure_late: float = -1.0  # ... and in the last part of the market from this bid (curve_take_sure_late)
    risk_take_sure_slide: float = -1.0  # ... or, sliding with the time left up to 1.00 at this share of the market (curve_take_sure_slide)
    risk_sure_logged: bool = False
    risk_sure_latched: bool = False  # the bet reached the sure level after the early window: its take stays held through a dip (released only by a fall past the market's own swing)
    risk_sure_level: float = 0.0
    risk_take_early_bid: float = -1.0  # sell at once at this bid in the first risk_take_early_seconds (lane field curve_take_early_bid)
    risk_take_early_seconds: float = 360.0
    risk_take_trail: float = 0.0  # the profit take's trail: typical minutes under the peak at which a bet that is ahead is sold (lane field curve_take_trail); 0 = off
    risk_take_trail_live: bool = False  # the trail sells (curve_take_trail_live); off = it logs and records a would-sell
    risk_take_trail_timed: bool = False  # the trail reads the time-aware typical minute (lane field curve_take_trail_timed), the static one when that has too little history
    risk_take_peak: float = 0.0  # the highest bid the risk check has seen on this bet
    risk_trail_logged: bool = False
    wave_logged: bool = False  # the wave salvage step said once what it would sell (a lane whose exit is not live)
    wave_peak_bid: float = 0.0  # the highest bid the wave_take step has seen on this bet since the buy
    wave_peak_ts: float = 0.0  # and when
    wave_take_logged: bool = False
    wave_failed: bool = False
    risk_time_to_go: float = 0.0  # seconds before the close in which time_to_go may sell (lane field curve_risk_time_to_go); 0 = off
    risk_time_to_go_chance: float = 0.5
    risk_time_to_go_wait_share: float = 0.25
    risk_go_since: float = 0.0  # when the curve chance first went under the bar and stayed (0 = not under)
    risk_late_tight: float = 0.0  # typical minutes under its high since the buy at which a bet is sold in the last share of the market, ahead or not (lane field curve_risk_late_tight); 0 = off
    risk_late_tight_share: float = 1.0 / 6.0  # the share of the market's 900 s at its end where late_tight applies
    risk_late_tight_live: bool = False
    risk_late_tight_logged: bool = False
    risk_under_even: float = 0.0  # the share of the time left a favorite's bid must stay under even before it is sold (lane field curve_risk_under_even); 0 = off
    risk_under_even_live: bool = False
    risk_under_since: float = 0.0  # when the bid last went under even and stayed
    risk_under_logged: bool = False
    risk_never_ahead: float = 0.0  # seconds left at which a bet never once in profit is sold (lane field curve_risk_never_ahead); 0 = off
    risk_ever_ahead: bool = False  # the bid less the sell fee has been over the cost at some risk check since the buy
    risk_first_seen: float = 0.0  # when the risk check first read this bet
    risk_never_state: int = 0  # 0 = the look has not happened, 1 = looked and kept, 2 = looked and sells
    risk_long_scales: tuple = ()  # the minutes the long layer reads its movement over, together (lane field curve_risk_long_scales); empty = the last hour
    risk_take_hist: list = field(default_factory=list)  # (time, bid) readings of the last minute, for the slope
    risk_score: bool = False  # the risk exit reads riskscore.py's chance in place of the net bid (lane field curve_risk_score)
    risk_at: float = 0.0  # the risk check reads at most once a second
    risk_noted: float = 0.0
    risk_since: float = 0.0  # when the risk began to stay high without a break (0 = not high)
    risk_hist: float = 1.0  # the market's last-hour history factor, reread every 30 s
    risk_hist_at: float = 0.0
    risk_confirm: float = -1.0  # lane curve_risk_confirm (-1 = off): a risk sale also needs the long layer's chance under this
    risk_long: float | None = None
    risk_long_at: float = 0.0
    risk_long_fallback: bool = False  # logged once: the long layer is read from the spot index because the perp is silent
    risk_long_missing: bool = False  # logged once: no long layer at all
    risk_logged: bool = False
    risk_held_kind: str = ""  # what last held a sale this bet's risk exit wanted ("long layer", "wait"), so the hold is logged once as it begins
    rest_price: float = 0.0  # the ask now working (0 = none)
    shock_index: str = ""  # the index it reads
    variety: str = ""  # the variants.py letter at the buy
    shock_ref: float = 0.0  # the price the cushion is measured from: the strike (crypto) or the index's own price at the window open (commodities)
    through_kept: bool = False  # the index came back to the winning side after a cross: hold this position to the close
    through_on: bool = False  # a cross has armed the watch and the index is still through
    checked: set[int] = field(default_factory=set)  # check-in marks (checkins.MARKS) already handled for this position
    index_id: str = ""  # the settlement index of this market
    cur_bid: float = 0.0  # the latest bid and the opposite side's ask, from the socket
    cur_other_ask: float = 0.0
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


def _burst_category(ticker: str) -> str:
    return "Crypto" if ticker.split("-")[0][2:].startswith(("BTC", "ETH", "SOL", "XRP", "DOGE")) else "Other"


def side_bid(side: str, yes_bid: float, yes_ask: float) -> float:
    """The price we could sell the contract we hold at, from a YES-book quote."""
    return yes_bid if side == "yes" else 1.0 - yes_ask


class ProfitWatcher:
    def __init__(
        self, get_settings: Callable[[], Settings], client: KalshiClient, store_factory: Callable[[], Store],
        hold_value: Callable[[Any, Store], float | None] | None = None,
        spot_side: Callable[[str, str], float | None] | None = None,
    ):
        self.spot_side = spot_side  # (ticker, side) -> the chance, from spot against the strike alone, that this side pays; None if unknown
        self._model = settlement.Model(lambda: self.tape_factory() if self.tape_factory is not None else None)  # the exact settlement chance (settlement.py)
        self._spot_src: dict[str, float] = {}  # index -> source time (ms) of the reading in _spot
        self._varieties: dict[tuple[str, str], str] = {}  # (ticker, lane) -> its variety at the buy
        self._shock_logged: set[tuple[str, str]] = set()  # shock exits already reported as armed or not
        self._shock_refs: dict[tuple[str, str], float] = {}  # (ticker, index) -> the price its shock exit measures the cushion from
        self._spot = self._model.spot  # settlement index -> (time, latest value), from the socket's index ticks
        self._tls = threading.local()
        self.hold_value = hold_value  # (open bet, store) -> the model's calibrated chance this position pays, or None if unknown
        self.get_settings = get_settings
        self.client = client
        self.client_for: Callable[[str], Any] | None = None  # lane -> the venue that holds it; Kalshi when unset
        self.manual = manual.ManualPositions(client)
        self.manual_rest = manualrest.ManualRest(client)
        self.store_factory = store_factory
        self._checks: queue.Queue[Any] = queue.Queue(maxsize=200)  # (target, mark, time, spot) waiting to be saved by checkins.py
        self.targets: dict[tuple[str, str], Target] = {}  # keyed by (ticker, lane)
        self._paper_rest_at = 0.0
        self._curve_at = 0.0
        self._curve_busy = False
        self._ledger_at = 0.0
        self._ticks_warm = False
        self._lock = threading.Lock()
        self._jobs: queue.SimpleQueue[SellSignal] = queue.SimpleQueue()  # sells for the sell worker. The socket only enqueues
        self._inflight: set[tuple[str, str]] = set()
        self._signalled: dict[tuple[str, str], float] = {}  # when each sell signal was queued, to time the sale (queue, checks, order)
        self.follow: Callable[[set[str]], None] | None = None  # start streaming these tickers now (the tape recorder's follow)
        self.tape_factory: Callable[[], Any] | None = None  # opens a read connection to the tape, for the entry context saved with a hand-placed buy
        self._tape: Any = None
        self._fills: queue.SimpleQueue[tuple[float, str, dict[str, Any]]] = queue.SimpleQueue()  # (fill time, ticker, the fill)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.sold: list[str] = []  # log lines, newest last (also logged)

    def covers(self, ticker: str, lane: str, beat: float = 0.0, now: float | None = None) -> bool:
        """True while the socket's price events are reaching this position, so its loss exits are decided here and nowhere else.

        The position needs a target that has already seen a quote, and either that quote or the stream's last event (`beat`, for a market
        that is simply quiet) is under SOCKET_QUIET seconds old. Otherwise the stream is not covering it and the sweep takes over."""
        now = time.time() if now is None else now
        with self._lock:
            t = self.targets.get((ticker, lane))
        return t is not None and t.last_ts > 0 and now - max(t.last_ts, beat) < SOCKET_QUIET

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
        if e.get("kind") in ("index", "index_fast") and e.get("index_id") and e.get("value"):
            idx, now = str(e["index_id"]), float(e["ts"])
            src = e.get("src_ms")
            if isinstance(src, (int, float)) and 0 < src < 1e11:
                src *= 1000  # a source time in seconds, not milliseconds
            elif not isinstance(src, (int, float)) or src <= 0:
                src = None
            if src is not None and src < self._spot_src.get(idx, 0):
                return  # the two index channels interleave; an older reading never replaces a newer one
            if src is not None:
                self._spot_src[idx] = src
            self._spot[idx] = (now, float(e["value"]))
            return
        if e.get("kind") != "quote" or e.get("bid") is None or e.get("ask") is None:
            return
        with self._lock:
            hits = [t for (ticker, _), t in self.targets.items() if ticker == e["ticker"]]
        if not hits:
            return
        with steptime.tick("sell"), memguard.decision("sell"):  # the sell decision of every bet on this market for this tick (budget: 60 ms; no database or Kalshi read in it)
            self._quote_hits(e, hits)
        line = steptime.tick_report()
        if line:
            log.info(line)

    def _quote_hits(self, e: dict[str, Any], hits: list) -> None:
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
            key = (t.ticker, t.lane)
            fast = self._fast_crash(t, bid)
            reason = self._stop_hit(t, bid, now) or fast or take_profit_hit(t.entry, bid, t.x, t.at) or self._unstable_hit(t, bid, now) or self._risk_quote_hit(t, bid, now)
            if reason and key not in self._inflight:
                self._inflight.add(key)
                self._queue_sell(SellSignal(t.ticker, t.lane, reason))



    def _market_static(self, ticker: str) -> dict[str, Any]:
        """A market read for its fixed fields (close time, strike): kept for ten minutes where the client can, so the 3-second refresh does not ask again."""
        reader = getattr(self.client, "market_static", None)
        return reader(ticker) if reader is not None else self.client.market(ticker)

    def _tape_db(self) -> Any:
        if getattr(self._tls, "tape", None) is None and self.tape_factory is not None:
            self._tls.tape = self.tape_factory()
        return getattr(self._tls, "tape", None)







    def _shock_reference(self, t: Target, index_id: str) -> float:
        """What the shock exit measures the cushion from. A BiQuote commodity price differs from Kalshi's strike by a dollar or two, so it is read against the same index's own price when the window opened (the tape); a crypto settlement index is read against the strike itself."""
        if not index_id.startswith(("BQ_", "KP_")):
            if t.strike <= 0:
                try:
                    t.strike = float(self._market_static(t.ticker).get("floor_strike") or 0.0)
                except (TypeError, ValueError, KalshiError):
                    return 0.0
            return t.strike
        if (t.ticker, index_id) in self._shock_refs:
            return self._shock_refs[(t.ticker, index_id)]
        tape = self._tape_db()
        if tape is None:
            return 0.0
        opened = t.close_ts - 900.0
        try:
            row = tape.execute("SELECT value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts LIMIT 1", (index_id, opened - 5.0, opened + 20.0)).fetchone()
        except Exception:
            return 0.0
        ref = float(row[0]) if row else 0.0
        if ref > 0:
            self._shock_refs[(t.ticker, index_id)] = ref
        return ref





    def _risk_hit(self, t: Target, bid: float, now: float, quote_ts: float) -> str | None:
        """The single risk exit on the spot's tick (curverisk.py, 2026-11-13, user), for a lane that gives the spot cushion a weight: the chance the side pays is the worse of Kalshi's own bid and the curve's spot chance (the price counted in full), widened by the market's last hour of jumps from its Kalshi perp. A lane with no spot weight is read on Kalshi's quote instead (_risk_quote_hit)."""
        if not t.sell_chain or t.risk_spot_weight <= 0 or t.close_ts <= 0 or t.shock_ref <= 0 or not t.shock_index or now - t.risk_at < 1.0 or time.time() < t.retry_after:
            return None
        left = t.close_ts - now
        if left <= CURVE_MIN_LEFT or bid <= 0 or now - quote_ts > 5.0:
            t.risk_since = 0.0
            return None
        t.risk_at = now
        got = ticks_mod.history.window(t.shock_index, now - 1000.0, now)
        read = curvevalue.read(got[0], got[1], now, t.shock_ref, t.side, left, t.curve_scales) if got else None
        if read is None:
            t.risk_since = 0.0
            return None
        if t.risk_history and now - t.risk_hist_at >= 30.0:
            kp = perp_index(t.ticker.split("-")[0])  # the Kalshi perp is the long-horizon layer, not the spot feed the cushion reads
            hour = ticks_mod.history.window(kp, now - 3700.0, now) if kp else None
            t.risk_hist, t.risk_hist_at = (curverisk.history_factor(hour[0], hour[1], now) if hour else 1.0), now
        net = bid - fee_per_contract(bid)
        basis = t.entry + fee_per_contract(t.entry)
        pnl = (net - basis) / basis if basis > 0 else 0.0
        r = curverisk.risk(read.p, net, t.risk_hist if t.risk_history else 1.0, t.risk_spot_weight)
        return self._risk_decide(t, r, bid, now, left, pnl, self._risk_long(t, now, left))

    def _take_cushion(self, t: Target, now: float, left: float, net: float) -> float | None:
        """The curve's chance, from the spot's distance to the strike over the time left, that the side pays: the profit take's cushion check (lane field curve_take_cushion). The reading is the lane's own (curvevalue.read on the index it is armed against), widened by the hour's jumps as the risk reading is. None when there is no reading (no spot index armed, too little history, a stale tick): the take then goes as before."""
        if t.close_ts <= 0 or t.shock_ref <= 0 or not t.shock_index or left <= 0:
            return None
        got = ticks_mod.history.window(t.shock_index, now - 1000.0, now)
        read = curvevalue.read(got[0], got[1], now, t.shock_ref, t.side, left, t.curve_scales) if got else None
        if read is None:
            return None
        return curverisk.risk(read.p, net, t.risk_hist if t.risk_history else 1.0, 0.0).q_spot

    def _risk_quote_hit(self, t: Target, bid: float, now: float) -> str | None:
        """The single risk exit on Kalshi's own quote (2026-11-13, user: "switch to use kalshis price instead of the external feeds for everything", then "exit first"): for a lane that gives the spot cushion no weight the chance the side pays IS the bid net of the sell fee, so the exit needs no spot feed, reads every quote and acts the second the price moves."""
        if not t.sell_chain or t.risk_spot_weight > 0 or t.close_ts <= 0 or now - t.risk_at < 1.0 or time.time() < t.retry_after:
            return None
        left = t.close_ts - now
        if left <= CURVE_MIN_LEFT or bid <= 0:
            t.risk_since = 0.0
            return None
        t.risk_at = now
        net = bid - fee_per_contract(bid)
        basis = t.entry + fee_per_contract(t.entry)
        pnl = (net - basis) / basis if basis > 0 else 0.0
        q = min(max(net, 0.0), 1.0)
        if getattr(t, "risk_score", False):
            scored = self._risk_score(t, now)
            if scored is not None:
                return self._risk_decide(t, curverisk.Risk(scored, q, scored, "score"), bid, now, left, pnl, self._risk_long(t, now, left))
        return self._risk_decide(t, curverisk.Risk(q, q, q, "price"), bid, now, left, pnl, self._risk_long(t, now, left))

    def _risk_score(self, t: Target, now: float) -> float | None:
        """The chance the side pays by riskscore.py (lane field curve_risk_score), or None when it cannot be read (the exit then reads the bid, as it always has)."""
        from . import riskscore

        try:
            got = riskscore.live(t.ticker.split("-")[0], t.ticker, t.side, t.close_ts, now, str(self.get_settings().data_dir), (0.0,))
        except Exception as exc:
            log.warning("risk score unreadable for %s %s: %s", t.ticker, t.lane, exc)
            return None
        return got[0][0] if got else None

    def reconcile_hand_sales(self, s: Settings, store: Store) -> list[str]:
        """Close the bot's record of a live bet the account no longer holds (the user sold it by hand, or bought the other side and Kalshi netted it), so the books agree and the lane may buy the market again. A shortfall has to show on two separate reads of the account, and the bot's last order on the market has to be 20 s old, because the positions read can trail a fill. Returns the log lines of what it closed."""
        out: list[str] = []
        if s.mode != "trade" or not getattr(self.client, "authed", False):
            return out
        self.manual.positions(s, store)  # reads the account at most every manual.POLL_EVERY seconds; the copy it keeps is what is compared here
        read_at = self.manual.read_at
        if read_at <= 0:
            return out
        account = self.manual.account
        with self._lock:
            watched = [(tk, lane, t) for (tk, lane), t in self.targets.items() if lane != manual.LANE]
        seen = getattr(self, "_short_seen", None)
        if seen is None:
            seen = self._short_seen = {}
        now = time.time()
        for tk, lane, t in watched:
            if not s.is_live(lane, tk.split("-")[0]):
                continue
            if t.close_ts <= 0 or now >= t.close_ts - CURVE_MIN_LEFT or t.cur_bid <= 0:
                seen.pop(tk, None)  # a market at or past its close leaves the account when it settles: that is not a hand sale (2026-11-13: two settled bets were closed "by hand" at a price of 0 half a minute after their close)
                continue
            held = store.net_contracts(tk, s.env)
            side, count = account.get(tk, (t.side, 0.0))
            real = count if side == t.side else 0.0
            if held < 0.05 or real >= held - 0.05:
                seen.pop(tk, None)
                continue
            first = seen.setdefault(tk, read_at)
            if read_at <= first:
                continue  # seen once: wait for a second, later read of the account to agree
            row = next((r for r in store.open_bets() if r["ticker"] == tk and (r["lane"] or "moonshot") == lane), None)
            if row is None:
                seen.pop(tk, None)
                continue
            line = runner.close_by_hand(s, store, row, real, t.cur_bid if t.cur_bid > 0 else 0.0)
            if line:
                log.info("watcher: %s", line)
                out.append(line)
                seen.pop(tk, None)
        return out

    def _risk_long(self, t: Target, now: float, left: float) -> float | None:
        """The long layer's chance that the side pays (curverisk.long_chance, from the market's Kalshi perp and its own last hour), only for a lane that asks for it (curve_risk_confirm), reread every second. None when the lane does not ask or the perp's hour is not there."""
        if t.risk_confirm <= 0:
            return None
        if now - t.risk_long_at < 1.0:  # 2026-11-15 night (user: "it's every 5 seconds then. Is that too frequent?"): the cushion in this chance is the perp's newest tick, so a reading 5 s old is 17% of the time left at 30 s to go. Re-read at the risk check's own pace, once a second: a few hundred lookups, about a millisecond per open bet
            return t.risk_long
        t.risk_long_at = now
        kp = perp_index(t.ticker.split("-")[0])
        reach = 60.0 * max(t.risk_long_scales or (60,)) + 100.0  # the history the scales need: the longest one and a minute's margin
        hour = ticks_mod.history.window(kp, now - max(3700.0, reach), now) if kp else None
        ref = ticks_mod.history.first_at_or_after(kp, t.close_ts - 900.0, 60.0) if kp else None
        val = curverisk.long_chance(hour[0], hour[1], now, ref or 0.0, t.side, left, scales=tuple(t.risk_long_scales), elapsed=max(0.0, 900.0 - left)) if hour else None
        if val is None and t.shock_index and t.shock_ref > 0:
            # The perp is silent or stale (a futures reference stops ticking outside its hours: KP_BRENT had one reading in 70 minutes on 2026-11-13 and the check fell away, so a 9-second dip sold a Brent bet on the price alone). Read the lane's own spot index against its armed reference instead.
            spot = ticks_mod.history.window(t.shock_index, now - 3700.0, now)
            val = curverisk.long_chance(spot[0], spot[1], now, t.shock_ref, t.side, left) if spot else None
            if val is not None and not t.risk_long_fallback:
                t.risk_long_fallback = True
                log.info("curve risk: %s %s reads its long layer from %s (the Kalshi perp %s has no fresh reading)", t.ticker, t.lane, t.shock_index, kp or "n/a")
        if val is None and not t.risk_long_missing:
            t.risk_long_missing = True
            log.warning("curve risk: %s %s has NO long layer (no fresh perp or spot hour): its sales go by the price alone", t.ticker, t.lane)
        t.risk_long = val
        return t.risk_long

    def _risk_unit(self, t: Target, now: float) -> float | None:
        """The market's own typical minute (quoteshock.typical_minute: sqrt(60) times the root-mean-square one-second change of its mid over the last 15 minutes), read from the quotes at most every 5 s and kept on the target."""
        if now - t.risk_unit_at >= 5.0:
            t.risk_unit_at = now
            t.risk_unit = quoteshock.typical_minute(ticks_mod.mids.rows(t.ticker), now)
        return t.risk_unit

    def _risk_unit_timed(self, t: Target, now: float, left: float) -> float | None:
        """The time-aware typical minute of the price (pricevol.implied_rate x sqrt(60)): the price's one-second move over the next 30 s implied by its own cushion and the time left, so it rises toward the close where _risk_unit stays a 15-minute average. Memory only, read at most every 5 s and kept on the target; None with too little history, and None (never an error) on any failure. SHADOW: only the trail_timed step reads it."""
        if now - t.risk_unit_timed_at >= 5.0:
            t.risk_unit_timed_at = now
            try:
                seq = pricevol.grid(ticks_mod.mids.rows(t.ticker), now)
                t.risk_unit_timed = pricevol.unit_of(pricevol.implied_rate(seq, left)) if seq is not None else None
            except Exception as e:
                t.risk_unit_timed = None
                if not t.trail_timed_failed:
                    t.trail_timed_failed = True
                    log.warning("curve risk shadow: the time-aware unit failed on %s %s: %r", t.ticker, t.lane, e)
        return t.risk_unit_timed

    def _risk_decide(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float, long_chance: float | None = None) -> str | None:
        """The sale decision both risk paths share: the watch line every 10 s, then the sale's steps in the bet's own order (sellchain.py; lane field sell_chain, or the default order: never ahead, under even, the profit take, the trail, the long layer's hold, the loss sale). A step answers a sale (returned), a hold (nothing is sold on this check) or nothing (the next step is asked). A lane with no chain gets the default order and its reasons as they always were; a lane with a chain gets the chain's note at the end of its sale reason."""
        if now - t.risk_noted >= 10.0:
            t.risk_noted = now
            log.info("curve risk watch %s %s, %.0fs left: chance %.3f (%s worse; price %.3f, spot %.3f, history x%.2f, long layer %s), result %+.1f%% of cost%s", t.ticker, t.lane, left, r.q, r.worse, r.q_price, r.q_spot, t.risk_hist, "n/a" if long_chance is None else f"{long_chance:.3f}", pnl * 100.0, "" if t.risk_live else " [shadow]")
        look = t.risk_take_slope * left  # the look back for the slope shrinks with the time left
        basis = t.entry + fee_per_contract(t.entry)
        net = bid - fee_per_contract(bid)
        c = _SellWalk(look, basis, net, long_chance)
        for name in t.sell_chain:
            got = _SELL[name](self, t, r, bid, now, left, pnl, c)
            if got is sellchain.HOLD:
                return None
            if got:
                return sellchain.tagged(got, t.sell_chain, t.sell_module, name)
        return None

    def _sell_never_ahead(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float, c: _SellWalk) -> str | sellchain._Hold | None:
        """Sell step never_ahead: the never-ahead sale (_risk_never_ahead), asked only of a lane that sets curve_risk_never_ahead."""
        net, basis = c.net, c.basis
        if getattr(t, "risk_never_ahead", 0.0) > 0:
            why = self._risk_never_ahead(t, r, bid, now, left, pnl, net, basis)
            if why:
                return why
        return None

    def _sell_under_even(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float, c: _SellWalk) -> str | sellchain._Hold | None:
        """Sell step under_even: the under-even sale (_risk_under_even), asked only of a lane that sets curve_risk_under_even."""
        if getattr(t, "risk_under_even", 0.0) > 0:
            why = self._risk_under_even(t, r, bid, now, left, pnl)
            if why:
                return why
        return None

    def _sell_shared(self, t: Target, bid: float, now: float, left: float, pnl: float, c: _SellWalk) -> None:
        """What the profit take, the trail and the long hold share, done once a check by the first of them that is asked (in the default order: after never_ahead and under_even, as before the chain): the bid goes into the slope's history and the squeeze allowance is read."""
        if c.shared:
            return
        c.shared = True
        look = c.look
        if t.risk_take:
            t.risk_take_hist.append((now, bid))
            while t.risk_take_hist and t.risk_take_hist[0][0] < now - max(60.0, 2.0 * look):
                t.risk_take_hist.pop(0)

    def _sell_take_state(self, t: Target, bid: float, now: float, left: float, c: _SellWalk) -> tuple[float, float, float, float | None, float]:
        """What the profit take and the trail share, done once a check by the first of them that is asked: the gain, the room, the fee to buy back, the market's typical minute, the take's bar, and the bet's high since the buy. (gain, room, rebuy, unit, bar)."""
        if not c.take_state:
            c.take_state = True
            net, basis = c.net, c.basis
            gain = net - basis
            room, rebuy = 1.0 - basis, fee_per_contract(bid)
            unit = self._risk_unit(t, now)
            bar = curverisk.take_bar(gain, room, left, 900.0, unit)
            t.risk_take_peak = max(t.risk_take_peak, bid)
            c.gain, c.room, c.rebuy, c.unit, c.bar = gain, room, rebuy, unit, bar
        return c.gain, c.room, c.rebuy, c.unit, c.bar

    def _sell_profit_take(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float, c: _SellWalk) -> str | sellchain._Hold | None:
        """Sell step profit_take (lane field curve_take): a gain over its bar is taken once the bid has stopped rising; with it the early-bid take, the sure-win latch and the cushion hold. In shadow (curve_risk_live off) it logs and records one would-sell and sells nothing."""
        ProfitWatcher._sell_shared(self, t, bid, now, left, pnl, c)
        if not t.risk_take:  # the profit take comes before the long layer: a bet that is well ahead is the one to cash, whatever the hour says
            return None
        gain, room, rebuy, unit, bar = ProfitWatcher._sell_take_state(self, t, bid, now, left, c)
        look, net = c.look, c.net
        wants = pnl > 0 and curverisk.take_profit(gain, room, left, 900.0, rebuy, unit)
        flat_ok = wants and (left <= 900.0 * curverisk.LATE_SHARE or bid >= curverisk.HIGH_BID) and curverisk.flat_holds(net, self._take_cushion(t, now, left, net), unit, left)  # in the last fifth a flat bid is held while the spot's cushion backs the price; a thin cushion sells (user, 2026-11-14)
        taken = wants and not curverisk.still_rising(t.risk_take_hist, now, look, unit, flat_ok)
        early = pnl > 0 and t.risk_take_early_bid > 0 and bid >= t.risk_take_early_bid and 900.0 - left <= t.risk_take_early_seconds
        if early:
            taken = True  # an early 0.95 is not a sure win: cash it, the re-entry buys back
        # The sure hold is a latch, not a line (2026-11-15 night, user: "if it drops to like 89 for a second it will sell? We don't want that behavior to be a hard stop"): once a bet has been a sure win, a dip does not end it. It is released only by a fall under the level that latched it by more than the market's own swing over the time left (the typical minute x sqrt(left / 60)), the same unit the take's late cap reads; with no reading of the unit it stays latched. The max-loss stop is untouched.
        if not early and curverisk.sure_win(bid, left, t.risk_take_sure, t.risk_take_sure_late, slide=t.risk_take_sure_slide):
            t.risk_sure_latched = True
            t.risk_sure_level = t.risk_take_sure if t.risk_take_sure > 0 and bid >= t.risk_take_sure else (curverisk.sure_bar(left, t.risk_take_sure_late, slide=t.risk_take_sure_slide) or t.risk_take_sure_late)
        elif t.risk_sure_latched and unit is not None and unit > 0 and left > 0 and bid < t.risk_sure_level - unit * math.sqrt(left / 60.0):
            t.risk_sure_latched = False
        if taken and not early and t.risk_sure_latched:
            taken = False  # a sure win: the bid is far enough up that the sale gives up cents to a dollar and pays a fee for protection the price already carries
            self._risk_held(t, r, bid, now, left, pnl, "sure hold", f"a profit take was wanted at a bid of {bid:.2f} and held: a sure win latched at {t.risk_sure_level:g}, held to settlement")  # a stored row (data/risk_shadow.jsonl), so the hold's effect can be scored on the bets it touched
            if not t.risk_sure_logged:
                t.risk_sure_logged = True
                log.info("curve risk: %s %s profit take held, a sure win (latched at %.2f): bid %.2f, %.0fs left; held to settlement through a dip, released only by a fall past the market's own swing", t.ticker, t.lane, t.risk_sure_level, bid, left)
        cushion = ""
        if early:
            cushion = f" [early take: the bid {bid:.2f} is at or over the lane's {t.risk_take_early_bid:g} with {900.0 - left:.0f}s of the market gone (the first {t.risk_take_early_seconds:.0f}s): cashed to be bought back]"
        elif taken and t.risk_take_cushion:
            chance = self._take_cushion(t, now, left, net)
            if chance is not None and curverisk.cushion_holds(chance, net, curverisk.CUSHION_NUDGE):
                taken = False  # the spot's cushion says the bet is worth more held than sold
                if not t.risk_take_held:
                    t.risk_take_held = True
                    log.info("curve risk: %s %s profit take held by the cushion: the spot's chance %.3f is over (or within the allowance of) the %.3f the sale pays (bid %.2f, %.0fs left)", t.ticker, t.lane, chance, net, bid, left)
            elif chance is not None:
                cushion = f" [cushion: the spot's chance {chance:.3f} is not within {curverisk.CUSHION_NUDGE:g} of the {net:.3f} the sale pays]"
            else:
                cushion = " [cushion: no spot reading, so the check did not apply]"  # 2026-11-14: SOL 21:45 sold with the check on and a chance over the bid; a sale has to say whether the check ran
        c.taken, c.early = taken, early  # the trail reads these when it is asked after this step
        if taken:
            used = 1.0 - left / 900.0
            reason = f"curve risk exit (profit take): the bet is {gain:.3f} a contract ahead, {gain / max(room, 1e-9) * 100.0:.0f}% of its {room:.3f} room against a bar of {bar / max(room, 1e-9) * 100.0:.0f}% ({'half the room: what is left to win is inside this market swing over the time left' if bar < used * room - 1e-9 else f'{used * 100.0:.0f}% of the market is gone'}, {left:.0f}s left), more than the {rebuy:.3f} fee to buy it back; the bid is no longer sloping up over {look:.0f}s ({t.risk_take_slope:.0%} of the time left; typical minute {t.risk_unit or 0.0:.3f}); sells at {bid:.2f} for {pnl * 100.0:+.1f}% of cost{cushion}"
            if t.risk_live:
                self._risk_record(t, r, bid, now, left, pnl, 0.0, False, True)
                return reason
            if not t.risk_logged:
                t.risk_logged = True
                log.info("curve risk shadow: %s %s would sell: %s", t.ticker, t.lane, reason)
                self._risk_record(t, r, bid, now, left, pnl, 0.0, False, False)
        return None

    def _sell_trail(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float, c: _SellWalk) -> str | sellchain._Hold | None:
        """Sell step trail (lane field curve_take_trail, on a lane with curve_take): a bet ahead is sold once its bid has come off its high since the buy by the market's own swing. It stands aside when the profit take, asked before it on this check, took the gain or took it early, and while the sure-win latch is set; with no profit_take step before it those are false."""
        ProfitWatcher._sell_shared(self, t, bid, now, left, pnl, c)
        if not t.risk_take:
            return None
        gain, _room, rebuy, unit, _bar = ProfitWatcher._sell_take_state(self, t, bid, now, left, c)
        taken, early = c.taken, c.early
        timed_note = ""
        if t.risk_take_trail_timed:  # the time-aware minute where there is one; the static minute when it has too little history (None, or an error already logged)
            timed = self._risk_unit_timed(t, now, left)
            if timed:
                unit, timed_note = timed, " time-aware"
        if not taken and not early and not t.risk_sure_latched and curverisk.trail_hit(gain, rebuy, bid, t.risk_take_peak, unit, t.risk_take_trail):
            # The trail: the bet is ahead and has come off its high by the market's own swing. It needs no bar (the bar asks for more the later it is, and a late buy starts under a high one), and the cushion check does not hold it: the replay it rests on had none.
            reason = f"curve risk exit (profit take): trail: the bet is {gain:.3f} a contract ahead and the bid {bid:.2f} is {t.risk_take_peak - bid:.2f} under its {t.risk_take_peak:.2f} high since the buy, at least {t.risk_take_trail:g}{timed_note} typical minute(s) of this market ({unit:.3f}); {left:.0f}s left"
            if t.risk_live and t.risk_take_trail_live:
                self._risk_record(t, r, bid, now, left, pnl, 0.0, False, True, kind="trail")
                return reason
            if not t.risk_trail_logged:
                t.risk_trail_logged = True
                log.info("curve risk shadow: %s %s would sell: %s", t.ticker, t.lane, reason)
                self._risk_record(t, r, bid, now, left, pnl, 0.0, False, False, kind="trail")
        return None

    def _sell_late_tight(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float, c: _SellWalk) -> str | sellchain._Hold | None:
        """Sell step late_tight (lane field curve_risk_late_tight; 2026-11-19, user: "i dont think high volatility means we don't buy, but i think it means we tighten our selling"; "build the new step as a module"). In the last share of the market (curve_risk_late_tight_share of its 900 s) a bet, ahead or not, is sold at the bid once its bid is curve_risk_late_tight typical minutes of its own market under its highest bid since the buy: the trail's question without the trail's need to be ahead. A sale by a step before it stands (put it after `trail`). It sells where curve_risk_late_tight_live and the risk exit are live; otherwise it logs one would-sell a bet and records it (kind "late tight" in data/risk_shadow.jsonl). It reads the bid, the high since the buy and the exit's typical minute: no model, no coin, no market. Needs the lane's profit take on (that keeps the high since the buy)."""
        if t.risk_late_tight <= 0 or not t.risk_take:
            return None
        ProfitWatcher._sell_shared(self, t, bid, now, left, pnl, c)
        _gain, _room, _rebuy, unit, _bar = ProfitWatcher._sell_take_state(self, t, bid, now, left, c)
        if not unit or unit <= 0 or left > 900.0 * t.risk_late_tight_share or t.risk_take_peak <= 0:
            return None
        if bid > t.risk_take_peak - t.risk_late_tight * unit + 1e-9:
            return None
        reason = f"curve risk exit (late tight): the bid {bid:.2f} is {t.risk_take_peak - bid:.2f} under its {t.risk_take_peak:.2f} high since the buy, at least {t.risk_late_tight:g} typical minute(s) of this market ({unit:.3f}), with {left:.0f}s left of the last {900.0 * t.risk_late_tight_share:.0f}s; sells at {bid:.2f} for {pnl * 100.0:+.1f}% of cost"
        extra = {"high": round(t.risk_take_peak, 4), "unit": round(unit, 5), "units": t.risk_late_tight}
        if t.risk_live and t.risk_late_tight_live:
            self._risk_record(t, r, bid, now, left, pnl, 0.0, False, True, kind="late tight", extra=extra)
            return reason
        if not t.risk_late_tight_logged:
            t.risk_late_tight_logged = True
            log.info("curve risk shadow: %s %s would sell: %s", t.ticker, t.lane, reason)
            self._risk_record(t, r, bid, now, left, pnl, 0.0, False, False, kind="late tight", extra=extra)
        return None

    TTG_DEFAULT = 15.0  # seconds of recovery assumed when the market's last hour has under 5 fast falls to measure
    TTG_MIN_LEFT = 8.0  # an order would land after the close

    def _sell_time_to_go(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float, c: _SellWalk) -> str | sellchain._Hold | None:
        """Sell step time_to_go (lane field curve_risk_time_to_go; the curve exit's time-to-go sale of 2026-11-17, user: "now lets. fix the curve exit, because WTI just landed 2 0s", as a step 2026-11-19): in the last curve_risk_time_to_go seconds a bet whose curve chance that the side pays is under curve_risk_time_to_go_chance is sold at the bid once that has held for a wait. TIME BASED: the chance must stay under the bar without a break for a wait that is at least the market's own median seconds for a fast fall to regain half over the last hour (15 s with under 5 falls) and at least curve_risk_time_to_go_wait_share of the time left; how deep the dip is never shortens it. A chance that comes back over the bar restarts the wait."""
        if t.risk_time_to_go <= 0 or bid <= 0 or not (self.TTG_MIN_LEFT <= left <= t.risk_time_to_go) or t.shock_ref <= 0 or not t.shock_index:
            t.risk_go_since = 0.0
            return None
        got = ticks_mod.history.window(t.shock_index, now - 1000.0, now)
        read = curvevalue.read(got[0], got[1], now, t.shock_ref, t.side, left, t.curve_scales) if got else None
        if read is None or read.p >= t.risk_time_to_go_chance:
            t.risk_go_since = 0.0
            return None
        tm = read.cushion / (read.rate_long * 60**0.5) if read.rate_long > 0 else 0.0
        base = self.TTG_DEFAULT
        stats = variants.comeback_stats(t.shock_index, now)
        if stats is not None and stats[1] >= 5:
            base = stats[2]
        wait = max(base, t.risk_time_to_go_wait_share * max(left, 0.0))  # time based (2026-11-19, user: "make it time based. that recent loss we had was unacceptable for it to have sold"): the old sale shortened its wait the deeper the dip and sold a one-moment dip (WTI 09:45 YES at 0.28, back to 0.81 in 75 s); here a dip never shortens the wait, which is at least the market's own recovery time and a share of the time left
        if t.risk_go_since <= 0:
            t.risk_go_since = now
        if now - t.risk_go_since < wait:
            return None
        reason = f"curve risk exit (time to go): the curve's chance is {read.p:.3f}, under {t.risk_time_to_go_chance:g}, with {left:.0f}s left; held {now - t.risk_go_since:.0f}s of a {wait:.0f}s wait that scales with the time left ({tm:+.2f} typical minutes under the target); bid {bid:.2f}"
        if t.risk_live:
            self._risk_record(t, r, bid, now, left, pnl, wait, False, True, kind="time to go", extra={"chance": round(read.p, 4), "cushion_units": round(tm, 3)})
            return reason
        return None

    def _sell_wave_take(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float, c: _SellWalk) -> str | sellchain._Hold | None:
        """Sell step wave_take (wavetop.py; 2026-11-22, user, on how they sell these: "aim for +20c or more and wait until the move slows" and "a resting sale at about +40c for spikes"). Two sales at the bid on a bet that is ahead: the bid has reached the entry plus RESTING (the resting sale would have filled), or it has reached the entry plus TAKE and has made no new high for a quarter of the run so far (the move has slowed: the wait scales with the run, never fixed seconds), never under 3 s. The bet's own price, not the spot, answers it."""
        if t.bought_ts <= 0 or bid <= 0 or t.entry <= 0:
            return None
        from . import wavetop

        if bid > t.wave_peak_bid + 1e-9:
            t.wave_peak_bid, t.wave_peak_ts = bid, now
        gain = bid - t.entry
        reason = ""
        if gain >= wavetop.RESTING - 1e-9:
            reason = f"wave take: the bid {bid:.2f} is {gain:+.2f} over the {t.entry:.2f} paid, the resting sale at +{wavetop.RESTING:.2f} fills"
        elif gain >= wavetop.TAKE - 1e-9:
            run = max(t.wave_peak_ts - t.bought_ts, 0.0)
            still = now - t.wave_peak_ts
            if still >= max(3.0, 0.25 * run):
                reason = f"wave take: the bid {bid:.2f} is {gain:+.2f} over the {t.entry:.2f} paid and has made no new high for {still:.0f} s (a quarter of the {run:.0f} s run is {0.25 * run:.0f} s): the move has slowed"
        if not reason:
            return None
        extra = {"gain": round(gain, 4), "peak": round(t.wave_peak_bid, 4), "still": round(now - t.wave_peak_ts, 1)}
        if t.risk_live:
            self._risk_record(t, r, bid, now, left, pnl, 0.0, False, True, kind="wave take", extra=extra)
            return reason
        if not t.wave_take_logged:
            t.wave_take_logged = True
            log.info("wave take shadow: %s %s would sell: %s", t.ticker, t.lane, reason)
            self._risk_record(t, r, bid, now, left, pnl, 0.0, False, False, kind="wave take", extra=extra)
        return None

    def _sell_wave_salvage(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float, c: _SellWalk) -> str | sellchain._Hold | None:
        """Sell step wave_salvage (wavetop.py; 2026-11-21, user: "we might be able to salvage this for a small profit or small loss ... a combination of the spot + kalshi price"): a bet that went under water is sold at the bid once it has come back off its low, its climb has slowed and the spot has stopped moving its way (wavetop.salvage). A bet that never dipped is held. The user's resting sale at the entry + 0.40 is only recorded, once (kind "wave rest"). Reads the quote and tick memory only. A failure is logged once and the step is passed over."""
        try:
            return ProfitWatcher._wave_salvage_read(self, t, r, bid, now, left, pnl)
        except Exception as e:
            if not t.wave_failed:
                t.wave_failed = True
                log.warning("wave salvage: the step failed on %s %s and is passed over: %r", t.ticker, t.lane, e)
        return None

    def _wave_salvage_read(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float) -> str | None:
        from . import binaryrisk, wavetop

        if t.close_ts <= 0 or t.bought_ts <= 0 or bid <= 0:
            return None
        index = binaryrisk.curve_index(t.ticker.split("-")[0])
        if not index:
            return None
        got = ticks_mod.history.window(index, now - 300.0, now)
        if not got or not got[0]:
            return None
        sell, info = wavetop.salvage(ticks_mod.mids.quotes(t.ticker), t.side, t.close_ts - 900.0, t.bought_ts, now, got[0], got[1])
        if not sell:
            return None
        reason = f"wave salvage: the bid fell to {info['low']:.2f} (its own typical minute {info['unit']:.3f} under the {info['entry_bid']:.2f} at the buy, or more), has come back to {bid:.2f}, its climb over the last {info['window']:.0f}s has slowed to {info['recent'] * 60:+.3f} a minute against the run's {info['run'] * 60:+.3f}, and the spot has stopped moving its way; sells at {bid:.2f} for {pnl * 100.0:+.1f}% of cost, {left:.0f}s left"
        extra = {k: round(v, 5) for k, v in info.items()}
        if t.risk_live:
            self._risk_record(t, r, bid, now, left, pnl, 0.0, False, True, kind="wave salvage", extra=extra)
            return reason
        if not t.wave_logged:
            t.wave_logged = True
            log.info("wave salvage shadow: %s %s would sell: %s", t.ticker, t.lane, reason)
            self._risk_record(t, r, bid, now, left, pnl, 0.0, False, False, kind="wave salvage", extra=extra)
        return None

    def _sell_trail_timed(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float, c: _SellWalk) -> str | sellchain._Hold | None:
        """Sell step trail_timed, SHADOW ONLY: it never sells and never holds (2026-11-18, user: "as time expires the volatility rate increases"; "so having a static volatility number is pointless"). It asks the trail's own question (curverisk.trail_hit, under the same conditions as _sell_trail) twice: with the exit's static typical minute and with the time-aware one (_risk_unit_timed), and writes one row (kind "trail_timed" in data/risk_shadow.jsonl) each time the pair of verdicts changes on the bet, with both units, both verdicts, the bet's high and `would_differ`. scripts/trail_timed_score.py scores the rows where they differ. Not in the default order: only a lane whose chain names it is asked. Memory only; an error is logged once a bet and the next step is asked."""
        try:
            ProfitWatcher._trail_timed_read(self, t, r, bid, now, left, pnl, c)
        except Exception as e:
            if not t.trail_timed_failed:
                t.trail_timed_failed = True
                log.warning("curve risk shadow: the trail_timed step failed on %s %s and is passed over: %r", t.ticker, t.lane, e)
        return None

    def _trail_timed_read(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float, c: _SellWalk) -> None:
        """The trail_timed step's body (see _sell_trail_timed, which catches whatever this raises)."""
        ProfitWatcher._sell_shared(self, t, bid, now, left, pnl, c)
        if not t.risk_take or t.risk_take_trail <= 0:
            return
        gain, _room, rebuy, unit, _bar = ProfitWatcher._sell_take_state(self, t, bid, now, left, c)
        timed = self._risk_unit_timed(t, now, left)
        stands = not c.taken and not c.early and not t.risk_sure_latched
        hit_static = stands and curverisk.trail_hit(gain, rebuy, bid, t.risk_take_peak, unit, t.risk_take_trail)
        hit_timed = stands and curverisk.trail_hit(gain, rebuy, bid, t.risk_take_peak, timed, t.risk_take_trail)
        pair = (bool(hit_static), bool(hit_timed))
        if pair == t.trail_timed_last:
            return
        t.trail_timed_last = pair
        self._risk_record(t, r, bid, now, left, pnl, 0.0, False, False, kind="trail_timed", extra={"unit_static": round(unit, 5) if unit else None, "unit_timed": round(timed, 5) if timed else None, "hit_static": pair[0], "hit_timed": pair[1], "would_differ": pair[0] != pair[1], "high": round(t.risk_take_peak, 4), "units": t.risk_take_trail})

    def _sell_long_hold(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float, c: _SellWalk) -> str | sellchain._Hold | None:
        """Sell step long_hold (lane field curve_risk_confirm): a HOLD. While the long layer's chance is at or over its bar the walk ends here with no sale, so no step after it is asked. (The loss step it used to guard was removed 2026-11-19; it holds the steps put after it.)"""
        ProfitWatcher._sell_shared(self, t, bid, now, left, pnl, c)
        long_chance = c.long_chance
        if t.risk_confirm > 0 and long_chance is not None and long_chance >= curverisk.confirm_bar(t.risk_confirm, left):
            return sellchain.HOLD
        return None

    def _sell_shape(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float, c: _SellWalk) -> str | sellchain._Hold | None:
        """Sell step shape, SHADOW ONLY: it never sells and never holds (2026-11-18, user: "if the path is wavy it means there's likely a comeback"; "if look back is trend or flat, and we're losing, we should probably exit"; "we need to test these concepts"). It keeps the bet's own bid path on every check, reads the path's shape (pathshape.shape) on two look-backs, since the buy and a quarter of the time left, adds each reading to its series' hour, and, while the bet is behind, writes one row to data/risk_shadow.jsonl (kind "shape") the first time each pair of shapes is seen on the bet. would_sell is the user's rule on the since-the-buy shape: trend against the bet or flat. Not in the default order: only a lane whose chain names it is asked. Memory only."""
        try:  # a shadow step must not be able to stop a sale: an error here is logged once a bet and the next step is asked
            ProfitWatcher._shape_read(self, t, r, bid, now, left, pnl)
        except Exception as e:
            if not getattr(t, "shape_failed", False):
                t.shape_failed = True
                log.warning("curve risk shadow: the shape step failed on %s %s and is passed over: %r", t.ticker, t.lane, e)
        return None

    @staticmethod
    def _shape_path_note(t: Target, bid: float, now: float) -> None:
        """Put this check's bid on the bet's path (shared by the shape steps; one entry a check even when both are asked)."""
        if not t.shape_path:  # the first check: the watcher's own last two minutes of this side's bids come first
            t.shape_path.extend((ts, b) for ts, b in t.history if ts < now and b > 0)
        if not t.shape_path or t.shape_path[-1][0] != now:
            t.shape_path.append((now, bid))

    def _sell_shape_timed(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float, c: _SellWalk) -> str | sellchain._Hold | None:
        """Sell step shape_timed, SHADOW ONLY: it never sells and never holds (2026-11-18, user: "they should be time based where it waits or has to exhibit the behavior for a certain time relative to the time remaining"). The `shape` step marks a bet the first moment it is behind with a flat or falling path; this one asks that the bet has stayed behind, and its path over that same stretch is flat or trending against, for pathshape.timed_window(left) seconds (a tenth of the time left, the time left counted as no less than 90 s). It writes one row (kind "shape_timed" in data/risk_shadow.jsonl) the first time both hold: `buy` is the shape over the window, `quarter` the shape since the buy. A bet that comes back ahead starts over. scripts/shape_shadow_score.py KIND=shape_timed scores it. Not in the default order; an error is logged once a bet and the next step is asked."""
        try:
            return ProfitWatcher._shape_timed_read(self, t, r, bid, now, left, pnl)
        except Exception as e:
            if not t.timed_failed:
                t.timed_failed = True
                log.warning("curve risk shadow: the shape_timed step failed on %s %s and is passed over: %r", t.ticker, t.lane, e)
        return None

    def _shape_timed_read(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float) -> str | None:
        """The shape_timed step's body (see _sell_shape_timed, which catches whatever this raises). Returns the sale's reason when the lane sells on it (a live exit module), else None."""
        ProfitWatcher._shape_path_note(t, bid, now)
        if pnl >= 0:
            t.timed_since = 0.0  # back ahead: the wait starts over
            return None
        if t.timed_since <= 0:
            t.timed_since = now
        live = bool(t.risk_live)
        if (t.timed_done and not live) or now - t.timed_at < pathshape.STEP:
            return None
        t.timed_at = now
        window = pathshape.timed_window(left)
        series = t.ticker.split("-")[0]
        unit = self._risk_unit(t, now)
        path = list(t.shape_path)
        got = pathshape.shape(path, now - window, now, unit, pathshape.seen_of(series, "timed", now))
        pathshape.note(series, "timed", now, got)
        behind_for = now - t.timed_since
        if behind_for < window or not pathshape.would_sell(got.shape):
            return None
        cushion = r.q - (bid - fee_per_contract(bid))  # the chance the exit acts on (the risk score's, on a lane that scores) less what the sale pays net of its fee: over zero = the model says the bet is worth more held than sold. On a lane without the score the chance IS that net bid, so the cushion is zero and a flat bet is held
        sells = live and got.shape == "trend_against"  # a falling bet is sold whatever the model says; a flat one is only recorded (2026-11-19: of 20 flat sales 15 bets won at settlement, the sale beat holding on 5)
        since = pathshape.shape(path, t.bought_ts, now, unit, pathshape.seen_of(series, "buy", now)) if t.bought_ts > 0 else pathshape.Shape("unknown")
        extra = {"would_sell": True, "buy": got.row(), "quarter": since.row(), "window": round(window, 1), "behind_for": round(behind_for, 1), "share": pathshape.TIMED_SHARE, "unit": round(unit, 5) if unit else None, "cushion": round(cushion, 4), "sold": sells}
        if not t.timed_done:
            t.timed_done = True
            log.info("curve risk %s: %s %s would sell (shape_timed): behind %+.1f%% of cost for %.0fs (a window of %.0fs, a tenth of the time left) and its path over that window is %s, cushion %+.3f; bid %.2f, %.0fs left", "live" if live else "shadow", t.ticker, t.lane, pnl * 100.0, behind_for, window, got.shape, cushion, bid, left)
            if not sells:
                self._risk_record(t, r, bid, now, left, pnl, 0.0, False, False, kind="shape_timed", extra=extra)
        if not sells:
            return None
        reason = f"curve risk exit (shape): the bet has been behind {pnl * 100.0:+.1f}% of cost for {behind_for:.0f}s, a tenth of the {left:.0f}s left or more, and its path over that window is {got.shape}" + f"; sells at {bid:.2f}"
        self._risk_record(t, r, bid, now, left, pnl, 0.0, False, True, kind="shape_timed", extra=extra)
        return reason

    def _shape_read(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float) -> None:
        """The shape step's body (see _sell_shape, which catches whatever this raises)."""
        ProfitWatcher._shape_path_note(t, bid, now)
        if now - t.shape_at < pathshape.STEP:
            return
        t.shape_at = now
        unit = self._risk_unit(t, now)
        series = t.ticker.split("-")[0]
        path = list(t.shape_path)
        got = {}
        for look, start in (("buy", t.bought_ts), ("quarter", now - pathshape.QUARTER * left)):
            got[look] = pathshape.shape(path, start, now, unit, pathshape.seen_of(series, look, now)) if start > 0 else pathshape.Shape("unknown")
            pathshape.note(series, look, now, got[look])
        pair = (got["buy"].shape, got["quarter"].shape)
        if pnl >= 0 or pair in t.shape_pairs:  # asked only of a bet that is behind; one row for each pair of shapes
            return
        t.shape_pairs.add(pair)
        would = pathshape.would_sell(got["buy"].shape)
        if would and not t.shape_would:
            t.shape_would = True
            b = got["buy"]
            log.info("curve risk shadow: %s %s would sell (shape): behind %+.1f%% of cost and its path since the buy is %s (net %+.3f over %.0fs, a typical move %.3f, path efficiency %.2f, %d reversals); the last quarter of the time left is %s; bid %.2f, %.0fs left", t.ticker, t.lane, pnl * 100.0, b.shape, b.net, b.window, b.typical, b.eff, b.reversals, got["quarter"].shape, bid, left)
        self._risk_record(t, r, bid, now, left, pnl, 0.0, False, False, kind="shape", extra={"would_sell": would, "buy": got["buy"].row(), "quarter": got["quarter"].row(), "unit": round(unit, 5) if unit else None})

    def _risk_under_even(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float) -> str | None:
        """The under-even sale (lane field curve_risk_under_even; 2026-11-17, user: "we need to fix this"). Every risk check notes whether the bid is under even; once it has been, without a break, for the lane's share of the time left, a bet bought as the favorite is sold at the bid (live), or logged and recorded once (shadow). A reading back at or over even starts the count again."""
        if bid >= curverisk.EVEN:
            t.risk_under_since = 0.0
            return None
        if t.risk_under_since <= 0:
            t.risk_under_since = now
        if not curverisk.under_even(bid, t.entry, now - t.risk_under_since, left, t.risk_under_even, self._risk_unit(t, now)):
            return None
        reason = f"curve risk exit (under even): the bid {bid:.2f} has been under even for {now - t.risk_under_since:.0f}s without a break, {t.risk_under_even:.0%} of the {left:.0f}s left or more, on a bet bought at {t.entry:.2f}; sells at {bid:.2f} for {pnl * 100.0:+.1f}% of cost"
        if t.risk_live and t.risk_under_even_live:
            self._risk_record(t, r, bid, now, left, pnl, t.risk_under_even * left, False, True, kind="under even")
            return reason
        if not t.risk_under_logged:
            t.risk_under_logged = True
            log.info("curve risk shadow: %s %s would sell: %s", t.ticker, t.lane, reason)
            self._risk_record(t, r, bid, now, left, pnl, t.risk_under_even * left, False, False, kind="under even")
        return None

    def _risk_never_ahead(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float, net: float, basis: float) -> str | None:
        """The never-ahead sale (lane field curve_risk_never_ahead; 2026-11-17, user: "never profitable and 90s left"). Every risk check notes whether the bet is in profit; at the lane's mark it looks once: a bet bought 30 s or more before the mark, watched the whole time and never once in profit is sold at the bid (when the risk exit is live; in shadow it is logged and recorded once). A bet that passes the look is never sold by this rule; one that fails keeps its sale signal until it is sold."""
        if t.risk_first_seen <= 0:
            t.risk_first_seen = now
        if net > basis:
            t.risk_ever_ahead = True
        if t.risk_never_state == 0 and left <= t.risk_never_ahead:
            bought_left = (15.0 - t.opened_min) * 60.0 if t.opened_min < 1e8 else -1.0  # what was left at the buy; unknown = the rule does not apply
            seen_whole = bought_left > 0 and t.risk_first_seen <= t.close_ts - bought_left + 30.0
            hit = curverisk.never_ahead(t.risk_ever_ahead, left, t.risk_never_ahead, bought_left, seen_whole)
            t.risk_never_state = 2 if hit else 1
            if not hit:
                log.info("curve risk: %s %s kept at the never-ahead look (%.0fs left): %s", t.ticker, t.lane, left, "it has been in profit since the buy" if t.risk_ever_ahead else "bought under 30 s before the mark" if 0 < bought_left < t.risk_never_ahead + 30.0 else "not watched from the buy (a restart or no buy time): its past is unknown")
        if t.risk_never_state != 2:
            return None
        reason = f"curve risk exit (never ahead): with {left:.0f}s left (the look is at {t.risk_never_ahead:.0f}s) the bet has not once been worth more than its {basis:.3f} cost since the buy; sells at {bid:.2f} ({net:.3f} after the fee) for {pnl * 100.0:+.1f}% of cost"
        if t.risk_live:
            self._risk_record(t, r, bid, now, left, pnl, 0.0, False, True, kind="never ahead")
            return reason
        t.risk_never_state = 1  # shadow: said once
        log.info("curve risk shadow: %s %s would sell: %s", t.ticker, t.lane, reason)
        self._risk_record(t, r, bid, now, left, pnl, 0.0, False, False, kind="never ahead")
        return None


    def _risk_held(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float, kind: str, why: str) -> None:
        """A live sale the risk exit wanted and something held (2026-11-15: four bets were held to a full loss with the bid at 0.37 to 0.41 when they crossed the max-loss line, and the log said nothing about why). Logged and written to data/risk_shadow.jsonl once as each kind of hold begins; it ends when the sale is no longer wanted."""
        if not t.risk_live or t.risk_held_kind == kind:
            return
        t.risk_held_kind = kind
        log.info("curve risk held %s %s, %.0fs left: %s (bid %.2f, %+.1f%% of cost, chance %.3f)", t.ticker, t.lane, left, why, bid, pnl * 100.0, r.q)
        self._risk_record(t, r, bid, now, left, pnl, 0.0, False, True, held=f"{kind}: {why}")

    def _risk_record(self, t: Target, r: curverisk.Risk, bid: float, now: float, left: float, pnl: float, wait: float, stop: bool, live: bool, held: str = "", kind: str = "", extra: dict[str, Any] | None = None) -> None:
        """Append one risk-exit signal to data/risk_shadow.jsonl (ticker, lane, bid, chance, result, time left, max-loss or line, live or shadow). Never raises."""
        try:
            from pathlib import Path

            row = {"ts": now, "ticker": t.ticker, "lane": t.lane, "side": t.side, "entry": t.entry, "bid": bid, "q": round(r.q, 4), "worse": r.worse, "pnl": round(pnl, 4), "left": round(left, 1), "wait": round(wait, 1), "stop": stop, "live": live}
            if held:
                row["held"] = held
            if kind:
                row["kind"] = kind
            if extra:
                row.update(extra)
            with open(Path(self.get_settings().data_dir) / "risk_shadow.jsonl", "a") as f:  # the watcher's own settings, not config.reload(): that rebinds the process-wide settings
                f.write(json.dumps(row) + "\n")
        except OSError:
            log.warning("curve risk: could not write data/risk_shadow.jsonl")


    COMEBACK_BACK_SECONDS = 120.0  # the window the comeback rate was measured over (variants.comeback_rate)













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
                store.touch("bets")
            else:
                store.db.execute(
                    "UPDATE decisions SET contracts=?, stake_usd=? WHERE id=?",
                    ((row["contracts"] or 0.0) + count, (row["stake_usd"] or 0.0) + stake, row["id"]),
                )
                store.db.commit()
                store.touch("bets")
            return
        store.add_decision(
            ticker=ticker, title="", bid=0.0, ask=round(paid, 4), model_p=0.0, edge=0.0,
            action="bet", reason="hand-placed moonshot" if paid <= s.manual_cheap_max else "hand-placed buy",
            mode="observe", close_time=close_time or "",
            lane=manual.LANE, category=_burst_category(ticker), side=side,
            contracts=count, stake_usd=stake,
        )


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
        if t.stop_drop <= 0 or bid <= 0 or t.entry <= 0:
            return None
        if time.time() < t.retry_after:
            return None
        ref = t.stop_ref or t.entry
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
            if t.close_ts > 0:
                mark = checkins.due(t.close_ts - now, t.checked)
                if mark is not None:
                    with contextlib.suppress(queue.Full):
                        self._checks.put_nowait((t, mark, now, self._spot.get(t.index_id) if t.index_id else None))  # the tape read happens on its own thread, never here
        for t in targets:
            key = (t.ticker, t.lane)
            if not t.last_ts or (t.trail_drop <= 0 and t.stop_drop <= 0):
                continue
            if now - t.bid_since >= t.trail_hold:
                t.peak = max(t.peak, t.last_bid)
            t.below_since = self._since(t, t.last_bid, now)
            key = (t.ticker, t.lane)
            fast = self._stop_hit(t, t.last_bid, now) or self._fast_crash(t, t.last_bid)
            if fast and key not in self._inflight:
                self._inflight.add(key)
                self._queue_sell(SellSignal(t.ticker, t.lane, fast))
                continue
            if t.below_since is not None and now - t.below_since >= t.trail_confirm and key not in self._inflight and now >= t.retry_after:
                why = None if below_floor(t, t.last_bid) else trail_hit(t.entry, t.peak, t.last_bid, arm_gain(t), crash_drop(t))
                if why:
                    self._inflight.add(key)
                    self._queue_sell(SellSignal(t.ticker, t.lane, f"{why} for {now - t.below_since:.0f}s"))

    # ---- housekeeping and the selling thread ----
    def refresh(self) -> None:
        """Reload which open bets to watch (new bets appear, exited ones go)."""
        s = self.get_settings()
        specs = lane_specs(s)
        store = self.store_factory()
        fresh: dict[tuple[str, str], Target] = {}
        for row in store.open_bets():
            spec = specs.get(row["lane"] or "moonshot")
            if spec is None or not spec.sell_chain:  # a lane sells only through its exit module's steps (sellchain.py); a bet of a lane with none is not watched
                continue
            ticker, lane = row["ticker"], row["lane"] or "moonshot"
            with self._lock:
                old = self.targets.get((ticker, lane))
            fresh[(ticker, lane)] = old or Target(ticker, lane, row["side"] or "yes", float(row["ask"] or 0.0), peak=float(row["ask"] or 0.0))
            held = fresh[(ticker, lane)]
            held.bought_ts = held.bought_ts or float(row["ts"] or 0.0)
            if not held.entry_filled:  # the price paid is what the buy filled at (a take chases the ask, a resting bid fills under it): the exits' "never a loss after both fees" is judged against it
                filled_at = store.entry_fill(ticker, held.side, float(row["ts"] or 0.0))
                if filled_at:
                    held.entry, held.entry_filled = filled_at, True
            if row["close_time"]:
                with contextlib.suppress(TypeError, ValueError):
                    close = datetime.fromisoformat(row["close_time"]).timestamp()
                    held.opened_min = 15.0 - (close - float(row["ts"])) / 60.0  # minutes into the 15-minute window when this buy was placed
                    if held.close_ts <= 0:
                        held.close_ts = close
            if held.close_ts > 0 and ticker.split("-")[0].endswith("15M"):  # the risk exit reads Kalshi's own quote: it needs no spot reference, so it is armed whether or not one exists (2026-11-13, user: "exit first")
                held.risk_live, held.risk_spot_weight, held.risk_history = spec.curve_risk_live, spec.curve_risk_spot_weight, spec.curve_risk_history
                held.sell_chain, held.sell_module = tuple(spec.sell_chain), spec.exit_module
                held.risk_min_profit, held.risk_confirm = spec.curve_risk_min_profit, spec.curve_risk_confirm
                held.risk_score = spec.curve_risk_score
                held.risk_take, held.risk_take_slope = spec.curve_take, spec.curve_take_slope_share
                held.risk_long_scales = tuple(spec.curve_risk_long_scales)
                held.risk_take_sure, held.risk_take_sure_late = spec.curve_take_sure, spec.curve_take_sure_late
                held.risk_take_sure_slide = spec.curve_take_sure_slide
                held.risk_take_early_bid, held.risk_take_early_seconds = spec.curve_take_early_bid, spec.curve_take_early_seconds
                held.risk_take_cushion = spec.curve_take_cushion
                held.risk_take_trail, held.risk_take_trail_live = spec.curve_take_trail, spec.curve_take_trail_live
                held.risk_take_trail_timed = spec.curve_take_trail_timed
                held.risk_never_ahead = spec.curve_risk_never_ahead
                held.risk_under_even, held.risk_under_even_live = spec.curve_risk_under_even, spec.curve_risk_under_even_live
                held.risk_time_to_go, held.risk_time_to_go_chance, held.risk_time_to_go_wait_share = spec.curve_risk_time_to_go, spec.curve_risk_time_to_go_chance, spec.curve_risk_time_to_go_wait_share
                held.risk_late_tight, held.risk_late_tight_share, held.risk_late_tight_live = spec.curve_risk_late_tight, spec.curve_risk_late_tight_share, spec.curve_risk_late_tight_live
                series_name = ticker.split("-")[0]
                sid = (variants.spot_index(series_name, spec.spot_source) if spec.spot_source == "kalshi" else None) or biquote_index(series_name) or index_for_series(series_name) or ""
                ref = self._shock_reference(held, sid) if sid else 0.0
                if (ticker, lane) not in self._shock_logged and (ref > 0 or sid):
                    self._shock_logged.add((ticker, lane))
                    if ref > 0:
                        log.info("watcher: the risk exit reads %s against %.4f on %s %s", sid, ref, ticker, lane)
                    else:
                        log.warning("watcher: the risk exit has NO spot reference on %s %s: no reference price for %s (%s)", ticker, lane, sid, "no tape" if self._tape_db() is None else "no tick near the window open")
                if ref > 0:
                    held.shock_index, held.shock_ref = sid, ref
                    held.curve_scales = spec.curve_exit_scales
        if s.manual_exit and getattr(self.client, "authed", False):
            self._add_manual(s, store, fresh)
            try:
                self._rest_manual(s, store, fresh)
            except Exception:
                log.warning("watcher: the resting sell on hand-placed positions failed this cycle", exc_info=True)
        for ticker, _lane in fresh:
            runner.arm_stop_history(s, ticker)  # one read of the last 15 minutes; live quotes extend it
        for held in fresh.values():
            if held.index_id:
                runner.arm_index_history(s, held.index_id)
        with self._lock:
            self.targets = fresh
            self._inflight.intersection_update(fresh)  # a sale signal for a market that has since ended
        try:
            self.reconcile_hand_sales(s, store)
        except Exception:
            log.warning("watcher: the hand-sale check failed this cycle", exc_info=True)
        if time.time() - self._ledger_at >= LEDGER_EVERY and any(sp.max_vol_rel > 0 for sp in lane_specs(s).values()):
            self._ledger_at = time.time()
            threading.Thread(target=self._refresh_ledger, args=(s,), daemon=True, name="variety-ledger").start()  # the scoreboard reads days of ticks: off the refresh cycle and at low priority
        if time.time() - self._curve_at >= 5.0 and not self._curve_busy:
            self._curve_at = time.time()
            self._curve_busy = True
            threading.Thread(target=self._refit_models, args=(s, lane_specs(s)), daemon=True, name="refit-models").start()  # off the refresh cycle
        if time.time() - self._paper_rest_at >= 10.0:
            self._paper_rest_at = time.time()
            try:
                got = paperfill.settle(s, store, self._tape_db())
                taker = paperfill.settle_taker(s, store, self._tape_db())
                got = {"filled": got["filled"] + taker["filled"], "missed": got["missed"] + taker["missed"]}
                if got["filled"] or got["missed"]:
                    log.info("watcher: paper resting buys judged: %d would have filled, %d would not", got["filled"], got["missed"])
            except Exception:
                log.debug("watcher: paper resting judgement failed", exc_info=True)

    def _refit_models(self, s: Settings, specs: dict[str, LaneSpec]) -> None:
        """Rebuild what a buy step reads from the tape, as its own low-priority work: the risk score's fit (risk_score and risk_trend steps) the curve model (curve_model step) and the state record (state_record step)."""
        try:
            if any(sp.enabled and sp.risk_score_buy > 0 for sp in specs.values()):
                from . import risktune

                risktune.maybe_refit(s)
            if any(sp.enabled and "curve_model" in sp.buy_chain for sp in specs.values()):
                from . import curvemodel

                curvemodel.maybe_refit(s)
            if any(sp.enabled and ("state_record" in sp.buy_chain or "state_record_shadow" in sp.buy_chain) for sp in specs.values()):
                from . import staterecord

                staterecord.maybe_refit(s)
            from . import binaryrisk

            if binaryrisk.wanted(specs):  # the shadow step or the deciding step
                binaryrisk.maybe_refit(s)
        except Exception:
            log.warning("watcher: refitting a buy step's model failed", exc_info=True)
        finally:
            self._curve_busy = False

    def _refresh_ledger(self, s: Settings) -> None:
        """Score the favorites that settled since the last pass and keep the tick history warm (variants.refresh_ledger, ticks.warm_from_tape). Background and low priority; a failure leaves what is there."""
        with contextlib.suppress(OSError):
            os.nice(10)
        try:
            indexes = sorted({ix for sp in lane_specs(s).values() for series in sp.series for ix in (index_for_series(series) or biquote_index(series), perp_index(series)) if ix})
            if not self._ticks_warm:  # a watcher that was never started (a script, a test): the start-up reload did not run
                self._ticks_warm = True
                ticks_mod.reload_from_tape(s, indexes, time.time())
            from . import volregime
            from .biquotefeed import SERIES_INDEX
            from .kalshiws import INDEX_FOR

            coins: dict[str, str | tuple[str, float]] = {variants.COINS[f"{prefix}15M"]: ix for prefix, ix in INDEX_FOR.items() if f"{prefix}15M" in variants.COINS}  # crypto: 6 h look-back
            coins.update({prefix[2:]: (ix, 4 * 3600.0) for prefix, ix in SERIES_INDEX.items()})  # commodities: 4 h (their feed is shorter), the figure the study used
            ratios = volregime.regime.update(s, coins)
            log.info("watcher: volatility regime %s", {c: (round(r, 2) if r else None) for c, r in ratios.items()})
            from . import marketprofile

            marketprofile.refresh(s, sorted({series for sp in lane_specs(s).values() if sp.enabled for series in sp.series if series.endswith("15M")}), tape=self._tape_db())
            from . import binaryrisk, wavetrend

            wave_series = sorted({series for sp in lane_specs(s).values() if sp.enabled and "wave_trend" in sp.buy_chain for series in sp.series})
            wavetrend.refresh(s, [ix for ix in (binaryrisk.curve_index(x) for x in wave_series) if ix], tape=self._tape_db())
            from . import lanehealth

            live = {name for name, sp in lane_specs(s).items() if sp.enabled and any(s.is_live(name, series) for series in sp.series)}
            lanehealth.check(s, Store(s.db_path, s.exclude_versions), live)
            from . import marketreview

            marketreview.run(s, Store(s.db_path, s.exclude_versions), marketprofile.snapshot())
            added = variants.refresh_ledger(s)
            if added:
                log.info("watcher: variety ledger +%d favorites (%d held)", added, len(variants.ledger))
        except Exception:
            log.warning("watcher: refreshing the variety ledger failed", exc_info=True)


    def _rest_manual(self, s: Settings, store: Store, fresh: dict[tuple[str, str], Target]) -> None:
        """The resting curve sell on hand-opened 15-minute positions (manualrest.py; MANUAL_REST_CURVE, 0 = off): an ask at the curve's chance plus a margin that
        shrinks with the time left, repriced every 15 s, and the time-to-go sale at the bid in the last MANUAL_REST_GUARD_SECONDS. While a position is on this rule
        the older profit-taking sales (the peak-crash trail) are declined in _sell_manual, since they would sell it at the bid first; a position whose curve cannot be read keeps the older rules."""
        rest = self.manual_rest
        held = {tk for (tk, lane) in fresh if lane == manual.LANE}
        rest.forget_gone(held)
        if s.manual_rest_far <= 0:
            for tk in list(rest.working):
                rest.cancel(tk)
            for (_tk, lane), t in fresh.items():
                if lane == manual.LANE:
                    t.rest_rule = False
            return
        live = s.mode == "trade" and (s.env != "prod" or s.allow_real_money)
        now = time.time()
        for p in self.manual.positions(s, store):
            t = fresh.get((p.ticker, manual.LANE))
            series = p.ticker.split("-")[0]
            if t is None or not series.endswith("15M"):
                continue
            try:
                left = datetime.fromisoformat(str(self._market_static(p.ticker).get("close_time", ""))).timestamp() - now
            except (TypeError, ValueError, KalshiError):
                continue
            bid = t.last_bid if t.last_ts and now - t.last_ts < 30.0 else 0.0
            read = variants.curve_edge_now(series, p.side, left, bid, ticks_mod.strikes.get(p.ticker) or store.window_strike(p.ticker), now)
            if read is None:
                t.rest_rule = False
                if p.ticker in rest.working:
                    rest.cancel(p.ticker)
                continue  # no curve, no rule: the older hand-placed rules stand
            t.rest_rule = True
            action, price = manualrest.plan(read[0], left, bid, s.manual_rest_far, s.manual_rest_guard)
            note = rest.step(p.ticker, p.side, p.count, action, price, left, live, now)
            if note:
                log.info("watcher: hand-placed %s %s: %s [curve chance %.2f, %.0f s left, bid %.2f]", p.ticker, p.side, note, read[0], left, bid)
            key = (p.ticker, manual.LANE)
            if action == "go" and now >= t.retry_after and key not in self._inflight:
                self._inflight.add(key)
                self._queue_sell(SellSignal(p.ticker, manual.LANE, f"{manualrest.GO_PREFIX}: {left:.0f} s left and the curve's chance {read[0]:.2f} is under one half; sold at the bid rather than held to a total loss."))

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
                    p.ticker, manual.LANE, p.side, p.entry, trail_activate=s.manual_exit_activate, trail_drop=s.manual_exit_drop,
                    trail_fast=s.manual_exit_fast, peak=p.entry,
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


    def _back_off(self, ticker: str, lane: str, seconds: float = RETRY_AFTER) -> None:
        """A declined sale is not re-checked for RETRY_AFTER seconds, so a position sitting under its price costs no API calls."""
        with self._lock:
            t = self.targets.get((ticker, lane))
        if t is not None:
            t.retry_after = time.time() + seconds


    def _spot_holds(self, t: Target | None, ticker: str, side: str, why: str, bid: float = 0.0) -> bool:
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

    def _queue_sell(self, sig: SellSignal) -> None:
        self._signalled[(sig.ticker, sig.lane)] = time.time()
        self._jobs.put(sig)

    def _sell(self, ticker: str, lane: str, why: str) -> None:
        picked = time.time()
        queued = self._signalled.pop((ticker, lane), picked)
        s = self.get_settings()
        store = self.store_factory()
        try:
            if lane == manual.LANE:
                self._sell_manual(s, store, ticker, why)
                return
            row = next((r for r in store.open_bets() if r["ticker"] == ticker and (r["lane"] or "moonshot") == lane), None)
            if row is None:
                return
            book = self._book(lane)
            m = book.market(ticker)
            read = time.time()
            if m.get("result") in {"yes", "no"}:
                return
            side = row["side"] or "yes"
            quoted = flip(m) if side == "no" else m
            bid = 0.0
            with contextlib.suppress(TypeError, ValueError):
                bid = float(quoted.get("yes_bid_dollars") or 0.0)
            entry = store.entry_fill(ticker, side, float(row["ts"] or 0.0)) or float(row["ask"] or 0.0)  # what the buy filled at: the decision ask understates it when the take chased the ask
            cut = why.startswith(("peak-crash", "stop-loss", "curve risk exit")) and not runner.is_profit_sale(why)
            if bid - fee_per_contract(bid) <= entry + fee_per_contract(entry) and not cut:  # a profit take never sells for a loss after both fees; the crash exit, the stop and the risk exit's sales do (the point is to get out)
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
            heal = (t.peak - crash_fast(t)) if (t is not None and "fast exit" in why) else (t.peak - crash_drop(t) if t is not None else 0.0)
            if t is not None and t.trail_drop > 0 and why.startswith("peak-crash") and bid > heal + 0.01:
                t.below_since = None  # the dip has already healed; keep riding
                self._back_off(ticker, lane)
                return
            # The socket is the freshest price (REST can show an older quote), so anchor the order to the lower of the two.
            if t is not None and t.last_ts and time.time() - t.last_ts < 5.0 and t.last_bid > 0:
                bid = min(bid, t.last_bid)
            slip = runner.EXIT_SLIP + (0.02 if "fast exit" in why else 0.0) + (0.03 if why.startswith("stop-loss") else 0.0)  # a collapse needs the surest fill
            ordering = time.time()
            line = runner.exit_position(s, book, store, row, bid, why + " [socket]", slip=slip)
            log.info("watcher: %s", line)
            log.info("watcher: sale timing %s: %.0f ms waiting in the queue, %.0f ms reading the market and checking, %.0f ms deciding to sell, %.0f ms placing the order",
                     ticker, (picked - queued) * 1000, (read - picked) * 1000, (ordering - read) * 1000, (time.time() - ordering) * 1000)
            self.sold.append(line)
            if t is not None:
                if line.startswith("partial exit 0/") or ": sell failed" in line:
                    t.sell_misses += 1  # nothing filled: wait 2, 4, 8 ... 60 s before the next try, so a sale that cannot fill is not sent a hundred times a minute
                    self._back_off(ticker, lane, seconds=min(60.0, 2.0 ** t.sell_misses))
                else:
                    t.sell_misses = 0
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
        go = why.startswith(manualrest.GO_PREFIX)  # the resting rule's late guard: a sale under cost, like a stop, but with no model to ask
        stop = why.startswith("stop-loss") or go
        if t is not None and t.rest_rule and not stop:
            self._back_off(ticker, manual.LANE)
            return  # the resting curve sell is working this position (manualrest.py): the older profit-taking sales stand down
        self.manual_rest.cancel(ticker)  # the ask holds these contracts: take it off the book before selling them another way
        if not go and self._spot_holds(t, ticker, pos.side, why):
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
            store.mark_exited(ticker, avg, why, manual.LANE, side=pos.side)
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

    def _run_checks(self) -> None:
        """Save the check-in rows (checkins.py). Slow reads of the tape live here so the one-second timer and the sell thread never wait on them."""
        store = None
        while not self._stop.is_set():
            try:
                t, mark, now, spot = self._checks.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                store = store or self.store_factory()
                row = checkins.build_row(t, mark, now, spot, checkins.snapshot(self.get_settings(), t))
                checkins.record(store, row)
            except Exception:
                log.exception("watcher: could not save a check-in for %s", getattr(t, "ticker", "?"))
                store = None

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

    def _reload_memory(self) -> None:
        """Fill memory from the tape as the first thing the process does (ticks.reload_from_tape), at normal priority. Until 2026-11-14 this ran inside the low-priority ledger job, only when a lane asked for that job, and finished 30 to 85 s after the bot was already deciding."""
        s = self.get_settings()
        indexes = sorted({ix for sp in lane_specs(s).values() for series in sp.series for ix in (index_for_series(series) or biquote_index(series), perp_index(series)) if ix})
        ticks_mod.reload_from_tape(s, indexes, time.time())

    def start(self) -> None:
        if self._threads:
            return
        self._ticks_warm = True
        ticks_mod.reload_begin()  # before any thread runs: buys wait for the reload from this moment (worker._evaluate)
        threading.Thread(target=self._reload_memory, name="memory-reload", daemon=True).start()
        for fn, name in (
            (self._run_refresh, "watch-refresh"), (self._run_sells, "watch-sell"), (self._run_timers, "watch-timer"),
            (self._run_fills, "watch-fills"), (self._run_checks, "watch-checks"),
        ):
            th = threading.Thread(target=fn, name=name, daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._stop.set()


_SELL = {name: getattr(ProfitWatcher, "_sell_" + name) for name in sellchain.SELL_STEPS}  # a sell step's name -> its method, taken off the class and called with the watcher handed in (as the risk exit's own tests call _risk_decide, on a stand-in watcher)


def wanted(s: Settings) -> bool:
    """True when hand-placed positions are watched, or any enabled lane has an exit module for the socket to run."""
    return s.manual_exit or any(spec.sell_chain for spec in lane_specs(s).values() if spec.enabled)

