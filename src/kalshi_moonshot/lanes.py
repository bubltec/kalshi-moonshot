"""Lanes as data. A lane is a set of parameters plus the name of the estimator that rates it.

Nothing about a lane is written into the code paths: the filter, the trend rule, the bet rule and
the order size all read a LaneSpec. The defaults come from the same environment settings as
before. A `strategies.toml` in DATA_DIR can change a lane's fields or add a whole new lane:

    [lane.metals_long]
    estimator = "ladder"
    price = [0.02, 0.30]
    hours = [1, 48]
    min_edge = 0.10
    series = ["KXGOLDD", "KXSILVERD"]

The file is read by lane_specs(), so editing it applies on the next scan.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass, fields, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import Settings

# Paper twin of the commodity lane. It reads Polymarket and never places an order, even if named in LIVE_LANES.
POLY_LANE = "commodity_poly"
# Every Polymarket lane. Paper unless that lane is named in LIVE_LANES. An empty list does not turn them on.
POLY_LANES = frozenset({POLY_LANE, "crypto_poly", "crypto_poly_5m"})

LLM = "llm"
LADDER = "ladder"
MARKET = "market"
CURVE = "curve"  # the class of the move so far in the window (curve.py) gives the chance
FORECAST = "forecast"  # the index's recent moves forecast the side's price (forecast.py)
SHAPE = "llm_shape"  # the language model reads the shapes of similar past 15-minute windows (shape.py)


@dataclass(frozen=True)
class LaneSpec:
    name: str
    estimator: str  # key in the estimator registry (estimators.py)
    label: str  # used in reject messages, e.g. "ask outside {label} range"
    price: tuple[float, float]  # ask range, dollars
    hours: tuple[float, float]  # hours to close range
    max_spread: float
    min_volume: float
    min_edge: float | None = None  # bet when the calibrated edge reaches this ...
    max_lag: float | None = None  # ... or, when set, when the estimate is no more than this under the ask
    usd_per_trade: float | None = None  # optional dollar ceiling per bet; None/0 = sized by balance
    fixed_stake: float = 0.0  # dollars per bet, ignoring Kelly (0 = size by the odds and the balance)
    always: bool = False  # bet whenever the filters pass, with no minimum edge from a model
    min_age_minutes: float = 0.0  # skip a market until it has been open this long (books settle down)
    gate_from: tuple[str, ...] = ()  # lanes whose finished bets decide which minutes of a 15-minute window this lane may enter (see gate.py)
    gate_since: str = ""  # a date (YYYY-MM-DD, New York time): only bets placed on or after it count toward the gate (a reset)
    gate_by: str = "band"  # "band": one record per minute band; "series": each market (series) keeps its own record, shrunk toward the lane's pooled band
    gate_pool: float = 30.0  # with gate_by="series": how many pooled bets' worth of prior a market's own record is blended with
    gate_min_n: int = 30  # finished bets a minute band needs before it can open
    gate_z: float = 1.0  # standard errors subtracted from a band's mean return before it must beat zero
    grid: bool = False  # buy only where the learned grid (grid.py) has a cell that pays; the lane's own exit settings manage the position
    knn: bool = False  # decide by the nearest-neighbour model of past entries (knn.py) instead of hard buckets
    knn_k: int = 300  # how many nearest past entries the estimate uses
    knn_min_n: float = 25.0  # comparable windows (after weighting) needed before it may buy
    knn_z: float = 3.0  # standard errors the estimate's mean must clear zero by
    knn_rule: str = "trail"  # the exit the past entries are scored under: "trail" (the live crash exit) or "hold"
    grid_rule: str = "trail"  # how the grid replays each entry: "trail" (the live peak-crash exit) or "hold" (to settlement, for a lane with no exit)
    grid_min_n: int = 30  # finished replays a cell needs before it can decide
    grid_z: float = 3.0  # standard errors a cell's mean must clear zero by (strict on purpose: of ~300 cells, some clear 2 by luck alone)
    scale: bool = False  # stake and exposure cap grow with the lane's own record (scale.py); the base stake until the record clears the bar
    scale_from: tuple[str, ...] = ()  # lanes whose finished bets count as the record (default: this lane)
    scale_since: str = ""  # only count bets opened on or after this New York date (YYYY-MM-DD)
    scale_min_n: int = 40  # finished bets needed before the stake can grow
    scale_z: float = 2.0  # standard errors the average return must clear zero by (one test per lane, not a search)
    scale_kelly: float = 0.25  # share of the Kelly stake the lower bound justifies
    scale_old_weight: float = 0.5  # how much the previous version's bets count, per version back (a new version is usually an improvement, so its record starts partly from the old one's, and fades from it as its own builds)
    scale_max_mult: float = 3.0  # most the stake and exposure cap may grow to, as a multiple of the base
    shares_markets: bool = False  # main-scan lane that also takes markets another lane took (for A/B controls); never consumes them
    min_chance: float = 0.0  # bet only when the estimator's chance that the contract pays is in [min_chance, max_chance]
    max_chance: float = 1.0  # (the estimate before calibration; for a ladder lane this is a distance to the strike)
    unstable_gain: float = 0.0  # sell when the position is up at least this fraction (after fees) AND the bid is swinging (see unstable_range); 0 = off
    unstable_range: float = 0.0  # ... the bid's high-low over unstable_seconds, in dollars
    unstable_seconds: float = 20.0
    hover_dip: float = 0.0  # sell at the first profit (after fees) once the bid has dipped to this price or lower since entry, i.e. hovered near the strike; 0 = off
    reentry_cooldown_seconds: float = 0.0  # after this lane sells a market, wait this long before it may buy that market again (0 = no wait)
    buy_rest_seconds: float = 0.0  # buy with a post-only RESTING order instead of an immediate one: it waits this many seconds at buy_rest_below under the ask and is dropped if unfilled (no fallback). A resting order that fills pays no taker fee; replay says it mostly fills on dips (0 = buy at the ask)
    buy_rest_below: float = 0.01  # how far under the ask the resting buy sits, in dollars
    max_open: int = 0  # at most this many bets of this lane open at once, across all its markets (0 = no limit); the next candidate waits until one closes
    min_dwell_seconds: float = 0.0  # buy a side only after its price has been inside the lane's band this long (the tape's quotes). Buying the moment it crosses 70c lost 28% on 85 bets; waiting 30 s+ about 4% (0 = buy at once)
    entry_trend_seconds: float = 0.0  # buy only when the side's price (mid of the tape's bid and ask, in its own terms) has not fallen over the last this many seconds by more than entry_trend_min (0 = no check). A market that is falling when we would buy lost 16-18% over 7 days; one flat or rising lost 0-5%
    entry_trend_min: float = 0.0  # the least the side's price must have changed over entry_trend_seconds, in dollars (0.0 = not falling; 0.01 = up a cent)
    entry_spot_ticks: int = 0  # buy only when the UNDERLYING's last this many price changes (the spot feed in the tape: an up-and-down market's YES is up, NO is down) all moved the way the bet goes, up for YES and down for NO (0 = off). Judged by the coin, not by the contract's own quote, which trails it. No spot price in the last 30 s means no opinion
    entry_spot_seconds: float = 0.0  # the same idea by time: the underlying's net move over the last this many seconds must be in the bet's direction (0 = off)
    entry_trend_z: float | None = None  # the same check in the market's own units: the move over entry_trend_seconds divided by that market's typical move over the same span (the standard deviation of its changes over the previous 2 minutes) must be MORE than this (0 = the side must be gaining; negative allows a fall of that many typical moves). Replaces entry_trend_min when set; None = off
    stop_loss_at: float = 0.0  # this lane's own stop, as a fraction under the price paid (0 = the global STOP_LOSS_DROP)
    stop_confirm_seconds: float = 0.0  # the LONGEST the stop waits for a dipped bid to recover before selling (0 = at once); the actual wait is calculated from the time left — strategy.stop_wait, fitted in scripts/stop_wait_study.py
    stop_guard: bool = False  # True = the model's opinion may hold the stop back; the default is a hard stop that sells whatever the model thinks it is worth
    stop_model: bool = False  # a 15-minute lane's stop first asks the model about the window's shape and holds only when it values the position above the sale (STOP_MODEL_SECONDS wait; no answer = sell)
    settlement_guard: float = 0.0  # OFF: 377 live sales, 88% would have won, -8.3c vs holding. Same job as the burst exit, worse signal. The watcher still has the code; 0.10 would turn it back on
    settlement_flip: bool = False  # watch held 15-minute bets for a volume burst against the holder in the last 120 s and SELL the favorite (watcher.py: that is the peak-crash exit); also log the other side as a paper bet in burst_flip. A silent book cannot 5x itself (BURST_MIN_NORMAL)
    burst_buy: float = 0.0  # dollars to put, for real, on the OTHER side (when it costs 5-25c) the first time that burst fires (0 = only log it). Needs settlement_flip and a live lane
    burst_dollars: float = 0.0  # the burst EXIT of a lane with settlement_flip, in dollars: sell the held 90c+ favorite in the last 120 s when at least this many dollars are bought against it in 3 s (0 = the older multiple-of-normal rule). Per family from burst_exit_study: crypto 400, currencies 50, metals/energy 400
    burst_dollars_btc: float = 0.0  # the same for BTC: a bar of its own, or -1 to keep the older rule (it beat holding by 5.3c on BTC; the dollar bar did not)
    hunt_dollars: float = 0.0  # a burst_hunt lane's trigger in dollars: a 90c+ favorite and at least this many dollars bought AGAINST it (the cheap side's takers) in 3 s, in the last 120 s; buy the cheap side. 0 = the older multiple-of-normal burst
    hunt_dollars_btc: float = 0.0  # the same bar for BTC, whose volume is far larger (0 = the same bar as the others)
    hunt_favorite: float = 0.0  # a burst_hunt dollar trigger buys the cheap side only when the OTHER side is at least this priced (0 = no requirement; 0.90 was the flip study's 90c+ favorite). price[1] is then the only band: a 20c side is an 80c favorite
    hunt_spike: float = 0.0  # a dollar trigger also needs the last 3 s of dollars bought on the cheap side to be at least this multiple of that side's own normal 3 s (median second of the 2 minutes before). 0 = the dollar bar alone. On a market that is busy all the time (BTC) a fixed bar fires on ordinary trading; a multiple fires on a surge (scripts/spike_trigger_study.py)
    hunt_cheap: float = 0.50  # the cheap side is a side priced under this
    hunt_window: float = 120.0  # a hunt acts when this many seconds or fewer remain...
    hunt_min_left: float = 10.0  # ...and at least this many (an order sent later lands after the close)
    hunt_crash: float = 0.10  # skip a side whose ask has fallen this much...
    hunt_crash_seconds: float = 12.0  # ...within this many seconds: a 60c favorite falling to 14c on the print is the crash, not a turn
    hunt_park_seconds: float = 20.0  # the ask must have been on screen this long...
    hunt_park_high: float = 0.10  # ...never above max(this, price[1])...
    hunt_park_span: float = 0.06  # ...and within this of the ask we would pay, so the price is parked, not still arriving
    hunt_attempts: int = 3  # a hunt buys a market at most this many times when each try certainly bought nothing (buyer.SENT_NOTHING); every try re-checks the whole trigger
    hunt_retry_seconds: float = 3.0  # seconds between those tries
    hunt_stale_seconds: float = 10.0  # a queued buy that waited this long is dropped: its price was read at the burst and the ask has moved since
    burst_favorite: float = 0.90  # the burst EXIT of a lane with settlement_flip and burst_dollars sells the held side only while its bid is at least this (a favorite)
    burst_share: float = 0.0  # the burst exit also needs this share of the last 3 s of dollars to have been bought AGAINST the held side (0 = the dollar bar alone). A burst where the held side is being bought as hard is two-way trading or a pump of the side we hold, not a crash (scripts/burst_floor_study.py)
    burst_window: float = 120.0  # ...in the last this many seconds. Past that, holding still paid (burst_flip record: last 120 s sold +6 to +8c vs holding -5 to -8c; at 2-5 min holding was still ahead)
    burst_min_left: float = 10.0  # ...and not in the last this many (a sale sent later lands after the close)
    require_prior_win: bool = False  # a burst_hunt lane buys a series for real only once one of its own bets on that series (live or paper) has already won; until then the series is logged as paper bets only, so it can earn its way in
    burst_hunt: bool = False  # watch this lane's 15-minute series in the last 120 s WITHOUT a position and buy a side in price[] when volume spikes (moonshot v2). Watcher-only; the main scan never sees it
    stop_from_seconds: float = 0.0  # the stop-loss acts only once this many seconds remain before the close (0 = whenever the hour rule allows). Replay of 6,299 favorites bets: holding through early dips until 3 minutes left helped commodity +1.2 and fx +1.1 points per dollar (within the error), cost crypto 1.1
    stop_loss: bool = True  # False = no stop-loss for this lane (a $1 ticket's loss is already capped; its spread alone can look like a 30% drop)
    take_profit_x: float = 0.0  # sell when the bid reaches this multiple of the price paid (0 = off); watched live on the socket
    take_profit_at: float = 0.0  # sell when the bid reaches this price (0 = off)
    cash_before_minute: float = 0.0  # a 15-minute buy opened before this minute of the window is cashed out early (0 = off; strategy.early_cash)
    cash_leave_minute: float = 9.0  # that buy is sold once the window reaches this minute, while the bid is still above the price paid
    cash_up_minute: float = 0.0  # once the window reaches this minute, sell if the bid is above the price paid (0 = off; strategy.cash_if_up)
    cash_drop: float = 0.0  # ...and sell when the bid is this fraction under the price paid (0.30 = down 30%). 0 = off. A smaller dip is left to the other exit
    cash_left_seconds: float = 0.0  # with this many seconds left, sell a hover that is still under the price paid (0 = off; strategy.cash_if_losing). A bid still above the price paid is held
    cash_at: float = 0.95  # or sooner, the moment the bid prints this
    watch_at: float = 0.0  # a favorite is watched once the bid prints this (0 = off; the moonshot lanes leave it off). A downtrend that stays there is sold after watch_ride_seconds, scaled by the time left (strategy.watch_trend). A hover or a spike is held
    watch_patient_left: float = 420.0  # watch_ride_seconds is the wait when at least this many seconds remain; less time left shortens the wait in proportion
    watch_ride_seconds: float = 0.0  # how long a visit under watch_at may last, at watch_patient_left, before a downtrend is abandoned (0 = a downtrend sells as soon as the last 3 seconds say so)
    watch_loss: float = 0.0  # hard stop: sell when the bid has lost this fraction of the price paid (0.75 keeps about a quarter). 0 = off. Checked the whole 15-minute window, ahead of the 45% line
    stop_cross_gap: float = 0.0  # a Polymarket lane: do not sell on a Polymarket bid that is more than this many dollars under the Kalshi bid for the same side and market (0 = off). Polymarket prints a bid of exactly 0.15 on markets Kalshi has at 0.75: the stops sold healthy bets into it
    watch_loss_seconds: float = 3.0  # once that line prints, this many seconds to bounce back above it
    scalp_at: float = 0.0  # once the bid prints this price, follow the high (0 = off). A climb keeps riding (strategy.scalp_hit)
    scalp_up: float = 0.0  # or once the bid is this far above the price paid (0 = off). Whichever arm is set and reached first
    scalp_dip: float = 0.03  # sell when the bid is this far under that high...
    scalp_dip_hard: float = 0.0  # a fall this far under the high sells at once, without waiting scalp_dip_seconds: in a sharp reversal the bid keeps falling while the wait runs (0 = off). 6c was best in scripts/scalp_dip_study.py
    scalp_dip_seconds: float = 3.0  # ...and has stayed that far under it for this many seconds (0 = on the first print). A dip that recovers sooner is noise, not a reversal
    rest_sell: bool = False  # rest the take-profit sell on the book from the moment a live buy fills (post-only, so no taker fee) instead of having the watcher cross the book when the bid gets there; needs take_profit_x or take_profit_at. A stop or crash exit cancels it first and sells what is left
    trail_activate: float = 0.0  # peak-crash exit, when trail_progress is 0: once the bid has been this fraction above the price paid (0.10 = up 10%) ...
    trail_progress: float | None = None  # arm the crash exit once the bid has covered this share of the way from the price paid to $1.00; None = the global CRASH_PROGRESS, 0 = use trail_activate (a gain on the price paid) instead
    trail_spot_sigma: float | None = None  # the crash exit waits while spot is still this many sigma on the winning side of the strike; None = the global CRASH_SPOT_SIGMA, 0 = off
    trail_giveback: float | None = None  # the crash exit sells once this share of the gain (peak over price paid) is gone; None = the global CRASH_GIVEBACK, 0 = the fixed trail_drop instead
    trail_drop: float = 0.0  # ... sell when it falls this many dollars from its peak (0 = off; watched live on the socket only)
    trail_fast_drop: float = 0.0  # ... but sell at once, with no wait, when the bid is this far (dollars) below the peak: a gap down is not noise (0 = off)
    trail_confirm_seconds: float = 10.0  # ... and has stayed that far down this long (a dip of a second or two is noise)
    trail_hold_seconds: float = 3.0  # a bid counts toward the peak only once it has held this long (one thin-book tick is not a peak)
    spike_cents: float = 0.0  # sell, while in profit, when the bid has risen this much (dollars) within spike_seconds (0 = off)
    spike_seconds: float = 30.0
    spike_min_gain: float = 0.10  # a spike sells only when the bid is also this fraction above the price paid (0.10 = up 10%)
    min_return: float = 0.0  # a win must pay at least this much on the stake, after the fee
    max_bet_pct: float = 0.02  # one bet, share of equity (see sizing.py)
    max_exposure_pct: float = 0.25  # all open bets in the lane, share of equity
    shortlist: int = 10
    numeric: str = "skip"  # "skip" | "allow" markets that resolve on a live number
    trend: str = "none"  # "none" | "rising" (needs history) | "not_falling" (no history is fine)
    one_per: str = "none"  # "none" | "mutex" (one side per exclusive event) | "event" (best edge only)
    series: tuple[str, ...] = ()  # restrict to these series; empty = any
    needs_instrument: bool = False  # series must be a priced commodity (instruments.toml)
    lookback_hours: int = 6  # candle window for the trend rule
    min_trend: float = 0.0  # ask gain over the window a 'rising' lane needs, dollars
    max_drop: float = 0.02  # ask fall (window or last candle) a 'not_falling' lane tolerates
    scope_minutes: float | None = None  # with review_minutes: slow pass that builds the watchlist
    review_minutes: float | None = None  # fast pass over the watchlist; None = the main scan only
    cooldown_minutes: float = 0.0  # skip a market decided on this recently
    entry_socket: bool = False  # two-level lanes: also re-review a watched event the moment a streamed quote puts a side in the price band (entrywatch.py)
    entry_approach: float = 0.0  # arm a side when its price is within this many dollars UNDER the lane's floor and gaining (entrywatch.py): the lane's review and shot are prepared then, and the buy goes out the moment the price reaches the floor (0 = off)
    entry_arm_seconds: float = 30.0  # an armed side that has not reached the floor within this long is dropped and may be armed again
    entry_min_gap_seconds: float = 2.0  # ... but not the same event more often than this
    buy_retries: int = 0  # when an immediate buy fills nothing, read the book again and send it once more, up to this many times, right away (0 = no retry). The next signal on the market comes a median 108 s later and about 3.7c dearer
    buy_retry_seconds: float = 5.0  # a retry only goes out within this many seconds of the first attempt
    buy_retry_chase: float = 0.0  # how many dollars over the first limit a retry may pay (0 = never above it). The lane's price band and the risk gate still apply
    buy_slip: float = 0.0  # the buy order is limited this many dollars above the ask the bot read: it still fills at the best ask, and pays up to the allowance only if the book has moved (0 = limit at the ask)
    watch_max: int = 40
    priority: int = 0  # queue priority of this lane's jobs; higher runs first
    trust: float = 1.0  # prior weight on the estimator before our record has any say (0 to 1)
    sides: tuple[str, ...] = ("yes",)  # contracts the lane may buy: "yes", "no" or both
    timed_suffix: str = ""  # series ending in this are fixed-length windows with an entry window
    timed_minutes: float = 15.0  # length of such a window
    entry_elapsed: tuple[float, float] = (0.0, 1e9)  # bet only this many minutes after it opens
    shadow_suffix: str = ""  # series ending in this are only measured (see shadow.py), never bet
    enabled: bool = True


def _pair(v: Any) -> tuple[float, float]:
    lo, hi = v
    return float(lo), float(hi)


def builtin_lanes(s: Settings) -> dict[str, LaneSpec]:
    """The three starting lanes, parameterized entirely from settings."""
    lanes = {
        "moonshot": LaneSpec(
            "moonshot", LLM, "long-shot", (s.min_price, s.max_price),
            (s.min_hours_to_close, s.max_hours_to_close), s.max_spread, s.min_volume,
            min_edge=s.min_edge, shortlist=s.shortlist, trend="not_falling", one_per="mutex",
            max_bet_pct=s.moonshot_bet_pct, max_exposure_pct=s.moonshot_exposure_pct,
            lookback_hours=s.steady_lookback_hours, max_drop=s.moonshot_max_drop,
            sides=s.moonshot_sides,
        ),
        "steady": LaneSpec(
            "steady", LLM, "steady", (s.steady_min_price, s.steady_max_price),
            (s.steady_min_hours_to_close, s.steady_max_hours_to_close), s.steady_max_spread,
            s.steady_min_volume, max_lag=s.steady_max_lag, usd_per_trade=s.steady_usd_per_trade or None,
            shortlist=s.steady_shortlist, trend="rising", one_per="mutex", enabled=s.steady_enabled,
            max_bet_pct=s.steady_bet_pct, max_exposure_pct=s.steady_exposure_pct,
            lookback_hours=s.steady_lookback_hours, min_trend=s.steady_min_trend,
            sides=s.steady_sides,
        ),
        "commodity": LaneSpec(
            "commodity", LADDER, "commodity", (s.commodity_min_price, s.commodity_max_price),
            (s.commodity_min_hours_to_close, s.commodity_max_hours_to_close),
            s.commodity_max_spread, s.commodity_min_volume, min_edge=s.commodity_min_edge,
            usd_per_trade=s.commodity_usd_per_trade or None, shortlist=s.commodity_shortlist,
            numeric="allow", one_per="event", needs_instrument=True, enabled=s.commodities_enabled,
            sides=("yes", "no"),
            scope_minutes=s.commodity_scope_minutes, review_minutes=s.commodity_review_minutes,
            cooldown_minutes=s.commodity_cooldown_minutes, watch_max=s.commodity_watch_max,
            priority=10, min_return=s.commodity_min_return, min_age_minutes=s.commodity_min_age_minutes, shadow_suffix="15M", timed_suffix="15M",
            entry_elapsed=(s.commodity_fast_from_min, s.commodity_fast_to_min),
            max_bet_pct=s.commodity_bet_pct, max_exposure_pct=s.commodity_exposure_pct,
        ),
        # Not scanned (disabled): worker.do_flip feeds it the opposite side of a market a stop-loss just sold, and the language model decides whether buying that side is worth it.
        "stop_flip": LaneSpec(
            "stop_flip", SHAPE, "stop_flip", (0.05, 0.95), (0.0, 1.5), 0.10, 0.0, min_edge=0.05, stop_loss=False, sides=("yes", "no"), enabled=False,
        ),
    }
    return lanes


def _overlay(base: LaneSpec | None, name: str, row: dict[str, Any]) -> LaneSpec:
    known = {f.name for f in fields(LaneSpec)}
    unknown = set(row) - known
    if unknown:
        raise ValueError(f"lane {name!r}: unknown field(s) {sorted(unknown)}")
    row = dict(row)
    for key in ("price", "hours", "entry_elapsed"):
        if key in row:
            row[key] = _pair(row[key])
    for key in ("series", "sides", "gate_from", "scale_from"):
        if key in row:
            row[key] = tuple(row[key])
    if bad := set(row.get("sides", ())) - {"yes", "no"}:
        raise ValueError(f"lane {name!r}: sides must be \"yes\" and/or \"no\", got {sorted(bad)}")
    if base is None:
        missing = {"estimator", "price", "hours"} - set(row)
        if missing:
            raise ValueError(f"lane {name!r}: new lanes need {sorted(missing)}")
        row.setdefault("label", name)
        row.setdefault("max_spread", 0.05)
        row.setdefault("min_volume", 0.0)
        return LaneSpec(name=name, **row)
    return replace(base, **row)


def _inherited(rows: dict[str, Any], name: str, _seen: tuple[str, ...] = ()) -> dict[str, Any]:
    """A lane's settings with `inherit = "other_lane"` resolved: the other lane's settings from strategies.toml, then this lane's own on top.
    Lets a paper twin say only how it differs. The chain may be several lanes long; a loop is an error."""
    row = dict(rows[name])
    parent = row.pop("inherit", None)
    if parent is None:
        return row
    if parent in _seen or parent == name:
        raise ValueError(f"lane {name!r}: inherit loops back through {' -> '.join((*_seen, name, parent))}")
    if parent not in rows:
        raise ValueError(f"lane {name!r}: inherit = {parent!r}, which is not a lane in strategies.toml")
    return {**_inherited(rows, parent, (*_seen, name)), **row}


def lane_specs_from(s: Settings, strategies_toml: str | None) -> dict[str, LaneSpec]:
    lanes = builtin_lanes(s)
    if strategies_toml:
        rows = tomllib.loads(strategies_toml).get("lane") or {}
        for name in rows:
            lanes[name] = _overlay(lanes.get(name), name, _inherited(rows, name))
    for spec in lanes.values():
        if tiered(spec) and not (spec.series or spec.needs_instrument):
            raise ValueError(
                f"lane {spec.name!r}: review_minutes needs `series` or `needs_instrument`, "
                "because a fast pass can only fetch what it can name"
            )
        if spec.entry_socket and not tiered(spec):
            raise ValueError(f"lane {spec.name!r}: entry_socket needs review_minutes, because it re-runs that lane's review")
    return lanes


@lru_cache(maxsize=8)
def _cached(s: Settings, text: str | None) -> dict[str, LaneSpec]:
    return lane_specs_from(s, text)


def strategies_path(data_dir: Path) -> Path:
    return data_dir / "strategies.toml"


def lane_specs(s: Settings) -> dict[str, LaneSpec]:
    """Lanes for these settings, with DATA_DIR/strategies.toml applied. Cached per file content."""
    path = strategies_path(s.data_dir)
    text = path.read_text() if path.is_file() else None
    return _cached(s, text)


def enabled_lanes(s: Settings) -> list[LaneSpec]:
    return [lane for lane in lane_specs(s).values() if lane.enabled]


def unknown_live_lanes(s: Settings) -> list[str]:
    """Names in LIVE_LANES that are not lanes. A typo there silently trades nothing, so it is reported."""
    return sorted((s.live_lanes or frozenset()) - set(lane_specs(s)))


def tiered(spec: LaneSpec) -> bool:
    """True for a two-level lane: scoped slowly into a watchlist, reviewed quickly from it."""
    return spec.review_minutes is not None


def main_lanes(s: Settings) -> list[LaneSpec]:
    """Enabled lanes served by the all-markets scan."""
    return [lane for lane in enabled_lanes(s) if not tiered(lane) and not lane.burst_hunt]


def tiered_lanes(s: Settings) -> list[LaneSpec]:
    return [lane for lane in enabled_lanes(s) if tiered(lane)]


def spec_for(s: Settings, name: str) -> LaneSpec:
    lanes = lane_specs(s)
    if name not in lanes:
        raise KeyError(f"unknown lane {name!r} (known: {', '.join(sorted(lanes))})")
    return lanes[name]


def stop_drop_for(spec: LaneSpec | None, s: Any) -> float:
    """How far under the price paid this lane's stop sits."""
    return spec.stop_loss_at if spec is not None and spec.stop_loss_at else s.stop_loss_drop


# Settings that only say how the bot goes about watching a lane, or how its record is read: changing one is not a change to what the lane does.
_NOT_LOGIC = frozenset({
    "name", "label", "enabled", "shortlist", "scope_minutes", "review_minutes", "priority", "watch_max", "cooldown_minutes", "shares_markets",
    "entry_socket", "entry_min_gap_seconds", "buy_slip", "buy_retries", "buy_retry_seconds", "buy_retry_chase",
    "scale", "scale_from", "scale_since", "scale_min_n", "scale_z", "scale_kelly", "scale_max_mult", "scale_old_weight", "gate_since",
})


def settings_of(spec: LaneSpec, s: Any) -> dict[str, str]:
    """Everything that decides what this lane buys, how much, and how it gets out, including the global rules that reach it."""
    out = {f.name: repr(getattr(spec, f.name)) for f in fields(spec) if f.name not in _NOT_LOGIC}
    out["effective_stop"] = repr(stop_drop_for(spec, s) if spec.stop_loss else None)
    out["crash_spot_sigma"] = repr(spec.trail_spot_sigma if spec.trail_spot_sigma is not None else s.crash_spot_sigma)
    out["crash_giveback"] = repr(spec.trail_giveback if spec.trail_giveback is not None else s.crash_giveback)
    progress = spec.trail_progress if spec.trail_progress is not None else s.crash_progress
    out["crash_progress"] = repr(progress)
    if spec.trail_drop > 0 and progress > 0:
        out["trail_activate"] = repr(0.1)  # arming follows the progress rule, so a declared trail_activate is unused; held at the value every lane carried so existing versions stay continuous
    out["stops_on_15m"] = repr(s.stop_loss_fast)
    out["kelly_fraction"] = repr(None if spec.fixed_stake else s.kelly_fraction)
    return out


def version_of(spec: LaneSpec, s: Any) -> str:
    """A short fingerprint of the lane's logic. Any change to it is a new version of the lane."""
    return hashlib.sha1(json.dumps(settings_of(spec, s), sort_keys=True).encode()).hexdigest()[:8]


def takes_profit(spec: LaneSpec) -> bool:
    return spec.settlement_guard > 0 or spec.take_profit_x > 0 or spec.take_profit_at > 0 or spec.unstable_gain > 0 or spec.hover_dip > 0 or spec.trail_drop > 0 or spec.spike_cents > 0 or spec.scalp_at > 0 or spec.scalp_up > 0 or spec.cash_up_minute > 0 or spec.cash_drop > 0 or spec.cash_left_seconds > 0


def chance_ok(spec: LaneSpec, p: float) -> bool:
    return spec.min_chance - 1e-9 <= p <= spec.max_chance + 1e-9


def wants_bet(spec: LaneSpec, p: float, ask: float, edge: float) -> bool:
    """The lane's bet rule, from its parameters. An edge bar and a lag bar can both be set."""
    if spec.always:
        return True
    if spec.max_lag is not None and ask - p > spec.max_lag + 1e-9:
        return False
    if spec.min_edge is not None and edge < spec.min_edge:
        return False
    return spec.max_lag is not None or spec.min_edge is not None
