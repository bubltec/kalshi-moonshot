"""Pure selection logic: which markets are long shots worth a model's time, and what a bet costs.

Nothing here talks to the network, so it is all unit tested.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .commodities import load_instruments, symbol_for
from .config import Settings
from .lanes import LaneSpec, spec_for

# Kalshi's general taker fee is about 7% of contracts * p * (1 - p), rounded up to the cent.
# Some series differ (see their fee schedule), so treat this as an estimate; live fills report
# the real fee. Edge checks use a slightly high rate to stay conservative.
FEE_RATE = 0.07


def fee_per_contract(price: float) -> float:
    return FEE_RATE * price * (1.0 - price)


def order_fee(price: float, count: float) -> float:
    return math.ceil(FEE_RATE * count * price * (1.0 - price) * 100.0 - 1e-9) / 100.0


@dataclass(frozen=True)
class LongShot:
    ticker: str
    title: str
    rules: str
    bid: float
    ask: float
    volume: float
    close_time: str
    hours_to_close: float
    subtitle: str = ""  # what YES means, e.g. the candidate's name in a multi-outcome event
    lane: str = "moonshot"
    series: str = ""
    trend: float | None = None  # ask change over the lookback window (steady lane)
    category: str = ""  # Kalshi event category, e.g. Sports, Politics
    event_ticker: str = ""
    mutually_exclusive: bool = False  # at most one YES in this event can occur
    side: str = "yes"  # contract bought; for "no", ask/bid are NO prices (1 - yes_bid / 1 - yes_ask)
    factors: str = ""  # JSON snapshot of the entry context, saved with the decision (factors.py); empty when the tape had none


def _f(m: dict[str, Any], key: str) -> float:
    try:
        return float(m.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _hours_until(iso: str, now: datetime) -> float | None:
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return (t - now).total_seconds() / 3600.0


NUMERIC_STRIKES = frozenset({"greater", "greater_or_equal", "less", "less_or_equal", "between"})
NUMERIC_REASON = "number threshold (model cannot see the live value)"


def is_numeric_threshold(m: dict[str, Any]) -> bool:
    """True for markets that resolve on a number: a price, a total, a count, an average.

    Kalshi marks them with strike_type greater/less/between and a floor or cap strike. The
    model has no way to know the current value, so its probability there is a guess. Team-win
    markets (structured) and date-based events (custom) are not numeric.
    """
    if m.get("strike_type") in NUMERIC_STRIKES:
        return True
    return m.get("floor_strike") is not None or m.get("cap_strike") is not None


def lane_reject_reason(m: dict[str, Any], spec: LaneSpec, s: Settings, now: datetime) -> str | None:
    """None if the market qualifies for this lane, else why not. Every bound comes from the spec."""
    skipped = {c.casefold() for c in s.skip_categories}
    category = (m.get("category") or "").casefold()
    if category and category in skipped:
        return "category skipped"
    series = _series(m)
    if spec.series and series not in spec.series:
        return "series not in this lane"
    if spec.needs_instrument and symbol_for(series, load_instruments(s.data_dir)) is None:
        return "not a supported commodity series"
    if spec.numeric == "skip" and s.skip_numeric and is_numeric_threshold(m):
        return NUMERIC_REASON
    if m.get("market_type", "binary") != "binary":
        return "not binary"
    if m.get("status") not in {"active", "open"}:
        return "not active"
    ask, bid = _f(m, "yes_ask_dollars"), _f(m, "yes_bid_dollars")
    if not (spec.price[0] <= ask <= spec.price[1]):
        return f"ask outside {spec.label} range"
    # Many cheap contracts have an ask but no bid and no trades. Nothing can be sold into that.
    if bid <= 0.0:
        return "no bid (cannot exit)"
    if spec.min_age_minutes and not (spec.timed_suffix and series.endswith(spec.timed_suffix)):
        # Right after a market opens its book is thin and its quotes jump around. Wait it out.
        # (15-minute windows have their own entry window, which already waits.)
        opened = m.get("open_time")
        if opened:
            try:
                age = (now - datetime.fromisoformat(opened)).total_seconds() / 60.0
            except (TypeError, ValueError):
                age = None
            if age is not None and age < spec.min_age_minutes:
                return f"market opened {age:.0f} min ago (waits {spec.min_age_minutes:g})"
    if spec.min_return and net_return(ask) < spec.min_return:
        return f"pays under {spec.min_return:.0%} if it wins"
    if ask - bid > spec.max_spread:
        return "spread too wide"
    if _f(m, "volume_fp") < spec.min_volume:
        return "volume too low"
    hours = _hours_until(m.get("close_time", ""), now)
    if hours is None:
        return "no close time"
    if spec.timed_suffix and series.endswith(spec.timed_suffix):
        # A fixed-length window opens exactly timed_minutes before it closes, so how far in we are
        # is known. Bet only inside the lane's entry window.
        elapsed = spec.timed_minutes - hours * 60.0
        if not spec.entry_elapsed[0] <= elapsed <= spec.entry_elapsed[1]:
            return f"outside the entry window ({elapsed:.1f} min into a {spec.timed_minutes:g}-min window)"
    if hours < spec.hours[0]:
        return "closes too soon"
    if hours > spec.hours[1]:
        return "closes too far out"
    return None


def flip(m: dict[str, Any]) -> dict[str, Any]:
    """The same market seen from the NO side: NO ask is 1 - YES bid, NO bid is 1 - YES ask.

    A missing quote stays missing (0), so the usual 'no bid' and price-range checks still apply.
    """
    ask, bid = _f(m, "yes_ask_dollars"), _f(m, "yes_bid_dollars")
    out = dict(m)
    out["yes_ask_dollars"] = f"{1.0 - bid:.4f}" if bid > 0.0 else "0"
    out["yes_bid_dollars"] = f"{1.0 - ask:.4f}" if 0.0 < ask < 1.0 else "0"
    return out


def side_view(m: dict[str, Any], side: str) -> dict[str, Any]:
    return m if side == "yes" else flip(m)


def side_prob(p_yes: float, side: str) -> float:
    """Chance the contract we hold pays, given the chance YES does."""
    return p_yes if side == "yes" else 1.0 - p_yes


def lane_candidates(
    markets: list[dict[str, Any]], spec: LaneSpec, s: Settings, now: datetime | None = None
) -> list[LongShot]:
    """Markets that pass the lane's filter, most traded first (tightest, least stale prices)."""
    now = now or datetime.now(UTC)
    out = [
        LongShot(
            ticker=m["ticker"],
            title=m.get("title", ""),
            rules=m.get("rules_primary", ""),
            bid=_f(view, "yes_bid_dollars"),
            ask=_f(view, "yes_ask_dollars"),
            volume=_f(m, "volume_fp"),
            close_time=m.get("close_time", ""),
            hours_to_close=_hours_until(m["close_time"], now) or 0.0,
            subtitle=m.get("yes_sub_title", "") or "",
            lane=spec.name,
            series=_series(m),
            category=m.get("category", "") or "",
            event_ticker=m.get("event_ticker", "") or "",
            mutually_exclusive=bool(m.get("mutually_exclusive")),
            side=side,
        )
        for m in markets
        for side in spec.sides
        for view in (side_view(m, side),)
        if lane_reject_reason(view, spec, s, now) is None
    ]
    out.sort(key=lambda x: x.volume, reverse=True)
    return out


# Named entry points kept for callers and tests; each is just a lane from the specs.
def reject_reason(m: dict[str, Any], s: Settings, now: datetime) -> str | None:
    return lane_reject_reason(m, spec_for(s, "moonshot"), s, now)


def long_shots(markets: list[dict[str, Any]], s: Settings, now: datetime | None = None):
    return lane_candidates(markets, spec_for(s, "moonshot"), s, now)


def _series(m: dict[str, Any]) -> str:
    return m.get("series_ticker") or str(m.get("event_ticker", "")).split("-")[0]


def gross_return(ask: float) -> float:
    """Profit as a fraction of cost if the contract pays $1 (before fees)."""
    return (1.0 - ask) / ask if ask > 0 else 0.0


def net_return(ask: float) -> float:
    """Profit as a fraction of cost if the contract pays $1, after the entry fee."""
    cost = ask + fee_per_contract(ask)
    return (1.0 - cost) / cost if cost > 0 else 0.0


# ---------------------------------------------------------------- steady lane

def steady_reject_reason(m: dict[str, Any], s: Settings, now: datetime) -> str | None:
    return lane_reject_reason(m, spec_for(s, "steady"), s, now)


def steady_candidates(
    markets: list[dict[str, Any]], s: Settings, now: datetime | None = None
) -> list[LongShot]:
    return lane_candidates(markets, spec_for(s, "steady"), s, now)


def commodity_reject_reason(m: dict[str, Any], s: Settings, now: datetime) -> str | None:
    return lane_reject_reason(m, spec_for(s, "commodity"), s, now)


def commodity_candidates(
    markets: list[dict[str, Any]], s: Settings, now: datetime | None = None
) -> list[LongShot]:
    return lane_candidates(markets, spec_for(s, "commodity"), s, now)


@dataclass(frozen=True)
class Trend:
    change: float  # last ask minus first ask in the window
    last_step: float  # last ask minus the one before it
    points: int


def ask_trend(candles: list[dict[str, Any]], side: str = "yes") -> Trend | None:
    """Ask-price trend of the contract we would buy. None when there is too little history.

    A NO ask is 1 - the YES bid, so for NO the candle's YES bid is flipped.
    """
    asks: list[float] = []
    for c in candles:
        try:
            if side == "yes":
                v = float((c.get("yes_ask") or {}).get("close_dollars") or 0.0)
            else:
                bid = float((c.get("yes_bid") or {}).get("close_dollars") or 0.0)
                v = 1.0 - bid if bid > 0.0 else 0.0
        except (TypeError, ValueError):
            continue
        if v > 0.0:
            asks.append(v)
    if len(asks) < 4:
        return None
    return Trend(change=asks[-1] - asks[0], last_step=asks[-1] - asks[-2], points=len(asks))


def rising(t: Trend, min_trend: float) -> bool:
    """Rising across the window and not rolling over right now."""
    return t.change >= min_trend and t.last_step >= 0.0


def trend_ok(t: Trend, s: Settings) -> bool:
    return rising(t, s.steady_min_trend)


def steady_model_ok(model_p: float, ask: float, max_lag: float) -> bool:
    """The rising ask is the signal. The model only vetoes when it is far below that price.

    A 0.55 on a 0.66 favorite is close enough. A 0.25 on a 0.70 favorite is the model
    calling a likely outcome a long shot, and that one is skipped.
    """
    return ask - model_p <= max_lag + 1e-9


FALLING_REASON = "ask is falling (live collapse, not a long shot)"


def stop_loss_hit(entry: float, bid: float, hours_left: float, hours: float, drop: float) -> str | None:
    """Why to sell now, or None to keep holding.

    Both conditions must hold: not much time left, and the bid is down `drop` or more
    from the price we paid. No bid means we cannot exit, so we hold.
    """
    if hours_left > hours or entry <= 0 or bid <= 0:
        return None
    lost = (entry - bid) / entry
    if lost < drop:
        return None
    return f"stop-loss: {hours_left:.1f}h left, down {lost:.0%} ({entry:.2f} -> {bid:.2f})"


def stop_wait(cap: float, seconds_left: float | None, fifteen_min: bool = False) -> float:
    """How long a dipped bid must stay past the stop line before the stop believes it, in seconds.

    Calculated, not guessed (scripts/stop_wait_study.py: all 7,200+ favorites bets replayed on their
    recorded quotes, 45% stop, sale resting at 1.1x). A dip with 7+ minutes left is information — the
    favorite is being repriced and does not come back (waiting cost up to 14 points of return). A dip
    with 2-5 minutes left is mostly the tape overreacting against the settlement average, and it does
    come back (waiting earned 7-16 points). Inside the last 2 minutes there is no extra wait: a sale
    happens only under 50c, and only when the last 3 seconds are a downtrend (strategy.price_shape).
    A spike or a hover is not a sale. Of the rules tried, the best (-1.4% a bet, against
    -1.8% for the cap-60 step rule, -2.0% for the flat 10 s before that, -2.2% at once, -2.6% no stop)
    sells at once past 7 minutes and waits longest — up to 120 s — at 2-3 minutes.

    `cap` is the lane's stop_confirm_seconds, the MOST the stop will ever wait; a lane that sets it to
    0 still sells at once. The early-out and the 120 s tier are 15-minute findings, so they apply only
    when `fifteen_min`; an hourly market (or an unknown close) past 7 minutes waits the cap, as before.
    """
    if cap <= 0:
        return 0.0
    if seconds_left is None:
        return cap
    if seconds_left > 420:
        return 0.0 if fifteen_min else cap
    if seconds_left > 180:
        return min(cap, 45.0)
    if seconds_left > 120:
        return min(cap, 120.0 if fifteen_min else 60.0)
    return 0.0  # last 2 minutes: no timer. price_shape says whether the bid under 50c is a trend


EARLY_BUY_MINUTE = 7.0


# A favorite bought before minute 7 is not held to settlement. While the bid is above the price paid
# and the last 3 seconds are a hover, it is sold. It is also sold at minute 9 if it is still ahead.
CASH_BEFORE_MINUTE = EARLY_BUY_MINUTE
CASH_LEAVE_MINUTE = 9.0


def _early(opened_min: float, before: float) -> bool:
    return before > 0 and 0 <= opened_min < before


def early_cash(
    entry: float, bid: float, opened_min: float, minute_now: float | None,
    *, before: float = CASH_BEFORE_MINUTE, leave: float = CASH_LEAVE_MINUTE, hovering: bool = False,
) -> str | None:
    """Why a buy opened before minute 7 should be sold now, or None. A later buy holds."""
    if not _early(opened_min, before) or entry <= 0 or bid <= entry:
        return None
    if hovering:
        return f"cash-out: bid {bid:.2f} is hovering above the {entry:.2f} paid (bought {opened_min:.0f} min in)"
    if minute_now is not None and minute_now >= leave:
        return f"cash-out: bought {opened_min:.0f} min in, selling {bid:.2f} at minute {minute_now:.0f} (paid {entry:.2f})"
    return None


def cash_if_up(entry: float, bid: float, minute_now: float | None, after: float, drop: float = 0.0) -> str | None:
    """Sell once the window reaches `after` minutes and the bid is above the price paid, or down `drop` of it.

    `after` is the lane's cash_up_minute and `drop` its cash_drop (0 = that side off). A smaller dip
    is left to the lane's other exit. None before that minute.
    """
    if after <= 0 or minute_now is None or minute_now < after or entry <= 0 or bid <= 0:
        return None
    if bid > entry:
        return f"cash-out: bid {bid:.2f} is above the {entry:.2f} paid at minute {minute_now:.0f}"
    if drop > 0 and (entry - bid) / entry >= drop - 1e-9:
        return f"cash-out: bid {bid:.2f} is {(entry - bid) / entry:.0%} under the {entry:.2f} paid at minute {minute_now:.0f}"
    return None


def cash_if_losing(entry: float, bid: float, seconds_left: float | None, within: float, hovering: bool) -> str | None:
    """Sell a hover that is still under the price paid once `within` seconds remain.

    `within` is the lane's cash_left_seconds (0 = off). The market is still quoting, and holding
    that hover to the result can settle as a total loss. A bid at or above the price paid is left
    to settle. A move that is not a hover is left to the lane's other exit.
    """
    if within <= 0 or not hovering or seconds_left is None or seconds_left < 0 or seconds_left > within:
        return None
    if entry <= 0 or bid <= 0 or bid >= entry:
        return None
    return f"cash-out: bid {bid:.2f} is under the {entry:.2f} paid with {seconds_left:.0f}s left"


def ride_for(seconds_left: float | None, ride: float, patient_left: float) -> float:
    """How long a visit under the watch line may last before a downtrend is sold.

    `ride` is that wait when at least `patient_left` seconds remain. Less time left shortens it in
    proportion, so both numbers are the lane's (strategies.toml) and not a fixed clock.
    """
    if ride <= 0 or seconds_left is None or seconds_left <= 0:
        return 0.0
    if patient_left <= 0 or seconds_left >= patient_left:
        return ride
    return ride * seconds_left / patient_left


def hard_loss(entry: float, bid: float, under_for: float, loss: float, bounce: float) -> str | None:
    """Sell once the bid has given back `loss` of the price paid and stayed there for `bounce` seconds.

    A print back above that line restarts `under_for`. None while the bounce is still open, or when
    `loss` is 0. Both numbers come from the lane.
    """
    if loss <= 0 or entry <= 0 or bid <= 0 or bid > entry * (1.0 - loss):
        return None
    if under_for < bounce:
        return None
    held = f" for {under_for:.0f}s" if bounce else ""
    return f"stop-loss: bid {bid:.2f} is {1 - bid / entry:.0%} under the {entry:.2f} paid{held} (watched)"


def scalp_hit(bid: float, high: float, arm: float, dip: float) -> str | None:
    """Sell once the bid has printed `arm` and then fallen `dip` dollars from the high since.

    A higher print raises that high, so a climb keeps riding. None until the arm has printed, or while
    the bid is still within `dip` of the high. `arm` and `dip` come from the lane.
    """
    if arm <= 0 or dip <= 0 or bid <= 0 or high < arm - 1e-9:
        return None
    if bid > high - dip + 1e-9:
        return None
    return f"scalp: bid {bid:.2f} is {high - bid:.2f} under the {high:.2f} high since {arm:.2f}"


def watch_trend(
    entry: float, bid: float, seconds_left: float | None, under_for: float,
    *, line: float, ride: float, patient_left: float, trend: bool,
) -> str | None:
    """Sell a favorite whose bid has stayed at or under `line` and is trending away.

    A hover or a spike is not a sale: the visit is only being watched. `under_for` is how long this
    visit has already lasted. The wait is ride_for. None while that wait is still running.
    A lane leaves this off by not setting `line` (the moonshot lanes).
    """
    if line <= 0 or seconds_left is None or seconds_left <= 0:
        return None
    if entry <= line or bid <= 0 or bid > line or not trend:
        return None
    wait = ride_for(seconds_left, ride, patient_left)
    if under_for < wait:
        return None
    held = f" for {under_for:.0f}s" if wait else ""
    return f"stop-loss: bid {bid:.2f} trending away under {line:.2f}{held} with {seconds_left:.0f}s left (paid {entry:.2f}) (watched)"


def price_shape(points: list[tuple[float, float]], now: float, bid: float) -> str:
    """What the last 3 seconds of bids are: 'trend', 'spike', or 'hover'.

    A hover barely moved (under 8c). A spike dropped and has already retraced at least half, or the
    whole drop is still a single gap. A trend stepped down at least twice and is still at the low.
    The stop sells only a trend, and only once the bid is under 50c. This reads the path already
    in hand; it does not wait another 3 seconds.
    """
    recent = [b for t, b in points if now - 3.0 <= t <= now and b > 0]
    if len(recent) < 2:
        return "hover"
    high, low = max(recent), min(recent)
    if high - low < 0.08:
        return "hover"
    if bid > low + 0.02 and (bid - low) >= 0.5 * (high - low):
        return "spike"
    steps = 0
    prev = recent[0]
    for price in recent[1:]:
        if price < prev - 0.02:
            steps += 1
        prev = price
    if steps >= 2 and bid <= low + 0.02:
        return "trend"
    return "spike"


def take_profit_hit(entry: float, bid: float, x: float = 0.0, at: float = 0.0) -> str | None:
    """Why to sell for a profit now, or None. `x` is a multiple of the price paid, `at` an absolute bid."""
    if entry <= 0 or bid <= 0:
        return None
    if x > 0 and entry * x < 1.0 and bid >= entry * x - 1e-9:
        return f"take-profit: bid {bid:.2f} is {bid / entry:.1f}x the {entry:.2f} paid"
    if at > 0 and entry < at and bid >= at - 1e-9:
        return f"take-profit: bid {bid:.2f} reached {at:.2f} (paid {entry:.2f})"
    return None


def trail_hit(entry: float, peak: float, bid: float, activate: float, drop: float) -> str | None:
    """Peak-crash exit: the bid has been `activate` (a fraction) above what we paid, and has now fallen `drop` from its peak.

    """
    if drop <= 0 or entry <= 0 or bid <= 0:
        return None
    if peak >= entry * (1 + activate) - 1e-9 and peak > entry and bid <= peak - drop + 1e-9:
        return f"peak-crash: bid {bid:.2f} is {peak - bid:.2f} off its {peak:.2f} peak (paid {entry:.2f})"
    return None


def spike_hit(
    history: list[tuple[float, float]], now: float, entry: float, cents: float, seconds: float, min_gain: float = 0.0
) -> str | None:
    """A sharp rise: the latest bid is `cents` above the lowest bid of the last `seconds`, and at least `min_gain` above what we paid.

    history is (time, bid) pairs, oldest first, the last being the latest.
    """
    if cents <= 0 or len(history) < 2:
        return None
    bid = history[-1][1]
    recent = [b for t, b in history if now - t <= seconds]
    if bid > entry * (1 + min_gain) - 1e-9 and bid > entry and len(recent) >= 2 and bid - min(recent) >= cents - 1e-9:
        return f"spike: bid {bid:.2f} is up {bid - min(recent):.2f} in {seconds:.0f}s (paid {entry:.2f})"
    return None


def falling_reason(t: Trend | None, min_drop: float) -> str | None:
    """Moonshot filter: a crashing ask is a live favorite going against us, not a long shot.

    No history is not a reject: a new cheap market can be a real long shot. A net drop, or a
    last-candle drop, of min_drop or more is.
    """
    if t is None:
        return None
    if t.change <= -min_drop or t.last_step <= -min_drop:
        return FALLING_REASON
    return None


def one_per_mutex_event(shots: list[LongShot]) -> list[LongShot]:
    """At most one market per mutually exclusive event, the cheapest ask (the long shot).

    Two YES contracts in the same game cannot both pay. Without this, a collapsing favorite
    and the underdog can both look like moonshots. Volume order is kept for the rest.
    """
    best: dict[str, LongShot] = {}
    rest: list[LongShot] = []
    for shot in shots:
        if not shot.mutually_exclusive or not shot.event_ticker:
            rest.append(shot)
            continue
        prev = best.get(shot.event_ticker)
        cheaper = prev is None or shot.ask < prev.ask - 1e-12
        same_and_busier = prev is not None and abs(shot.ask - prev.ask) <= 1e-12 and shot.volume > prev.volume
        if cheaper or same_and_busier:
            best[shot.event_ticker] = shot
    out = rest + list(best.values())
    out.sort(key=lambda x: x.volume, reverse=True)
    return out


def edge(model_p: float, ask: float) -> float:
    """Expected profit per $1 payout contract after the entry fee."""
    return model_p - ask - fee_per_contract(ask)


def side_ask_notional(book: dict[str, Any], limit: float, side: str = "yes") -> float:
    """Dollars of the given contract for sale at or under limit (its own price).

    Kalshi's book lists bids only. A NO bid at 0.97 is a YES ask at 0.03, and a YES bid at 0.97
    is a NO ask at 0.03.
    """
    if side == "yes":
        return yes_ask_notional(book, limit)
    raw = book.get("orderbook_fp") or book
    total = 0.0
    for row in raw.get("yes_dollars") or []:
        try:
            yes_px, qty = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        no_px = 1.0 - yes_px
        if 0 < no_px <= limit + 1e-9:
            total += no_px * qty
    return total


def best_bid(book: dict[str, Any], side: str = "yes") -> float | None:
    """The highest price anyone bids for the given contract right now (what a sale of it would get), from the order book; None when nobody does.
    Kalshi's book lists bids for each contract: YES bids under yes_dollars, NO bids under no_dollars."""
    raw = book.get("orderbook_fp") or book
    best = None
    for row in raw.get("yes_dollars" if side == "yes" else "no_dollars") or []:
        try:
            px, qty = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if qty > 0 and px > 0 and (best is None or px > best):
            best = px
    return best


def yes_ask_notional(book: dict[str, Any], limit: float) -> float:
    """Dollars of YES for sale at or under limit.

    Kalshi's book lists NO bids. A NO bid at 0.97 is a YES ask at 0.03.
    """
    raw = book.get("orderbook_fp") or book
    total = 0.0
    for row in raw.get("no_dollars") or []:
        try:
            no_px, qty = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        yes_px = 1.0 - no_px
        if 0 < yes_px <= limit + 1e-9:
            total += yes_px * qty
    return total


def contracts_for_budget(budget_usd: float, ask: float, fractional: bool = False) -> float:
    """How many contracts fit in the budget, counting the fee. Whole contracts, or with `fractional` to the hundredth, so the whole budget is spent."""
    if ask <= 0:
        return 0
    if fractional:
        n = math.floor(budget_usd / (ask + fee_per_contract(ask)) * 100.0 + 1e-9) / 100.0
        while n > 0 and ask * n + order_fee(ask, n) > budget_usd + 1e-9:
            n = round(n - 0.01, 2)
        return max(n, 0.0)
    n = int(budget_usd / (ask + fee_per_contract(ask)))
    while n > 0 and ask * n + order_fee(ask, n) > budget_usd:
        n -= 1
    return max(n, 0)


# ---------------------------------------------------------------- guaranteed-profit scan

_LADDER = re.compile(r"-T-?\d+(\.\d+)?$")


@dataclass(frozen=True)
class Arb:
    event_ticker: str
    low: str  # buy YES on this (lower strike) ...
    high: str  # ... and NO on this (higher strike)
    cost: float  # both legs, with fees
    floor_payout: float  # what the pair pays in the worst case
    net: float  # worst-case profit per contract pair
    size: float  # contracts both sides can support (smaller lifetime volume; a rough cap)


def ladder_arbs(markets: list[dict[str, Any]]) -> list[Arb]:
    """Guaranteed-profit pairs inside a strike ladder, after fees.

    For one quantity at strikes K1 < K2, 'above K1' is true whenever 'above K2' is. Buying YES on
    K1 and NO on K2 pays $1 if the price ends below K1 or above K2, and $2 in between, so at
    least $1 always. It is free money when YES(K1)'s ask plus NO(K2)'s ask (1 - YES(K2)'s bid)
    plus both fees is under $1, i.e. when the lower strike's ask sits below the higher strike's bid.

    Only tickers ending in -T<number> within one event count: that suffix is Kalshi's ladder on a
    single quantity. Spread, team and bracket markets do not nest, and treating them as if they do
    produces phantom arbitrage.
    """
    import itertools

    by_event: dict[str, list[dict[str, Any]]] = {}
    for m in markets:
        if (
            m.get("strike_type") in {"greater", "greater_or_equal"}
            and m.get("floor_strike") is not None
            and _LADDER.search(m.get("ticker", ""))
        ):
            by_event.setdefault(m.get("event_ticker", ""), []).append(m)
    out: list[Arb] = []
    for event, group in by_event.items():
        group.sort(key=lambda m: float(m["floor_strike"]))
        for lo, hi in itertools.combinations(group, 2):
            if float(lo["floor_strike"]) == float(hi["floor_strike"]):
                continue
            ask, bid = _f(lo, "yes_ask_dollars"), _f(hi, "yes_bid_dollars")
            if ask <= 0.0 or bid <= 0.0:
                continue
            cost = ask + fee_per_contract(ask) + (1.0 - bid) + fee_per_contract(1.0 - bid)
            if cost < 1.0:
                out.append(Arb(
                    event, lo["ticker"], hi["ticker"], cost, 1.0, 1.0 - cost,
                    min(_f(lo, "volume_fp"), _f(hi, "volume_fp")),
                ))
    out.sort(key=lambda a: a.net, reverse=True)
    return out
