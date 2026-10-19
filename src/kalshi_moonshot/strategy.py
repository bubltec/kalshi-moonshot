"""Pure selection logic: which markets are long shots worth a model's time, and what a bet costs.

Nothing here talks to the network, so it is all unit tested.
"""

from __future__ import annotations

import math
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


def order_fee(price: float, count: int) -> float:
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
    if ask - bid > spec.max_spread:
        return "spread too wide"
    if _f(m, "volume_fp") < spec.min_volume:
        return "volume too low"
    hours = _hours_until(m.get("close_time", ""), now)
    if hours is None:
        return "no close time"
    if hours < spec.hours[0]:
        return "closes too soon"
    if hours > spec.hours[1]:
        return "closes too far out"
    return None


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
            bid=_f(m, "yes_bid_dollars"),
            ask=_f(m, "yes_ask_dollars"),
            volume=_f(m, "volume_fp"),
            close_time=m.get("close_time", ""),
            hours_to_close=_hours_until(m["close_time"], now) or 0.0,
            subtitle=m.get("yes_sub_title", "") or "",
            lane=spec.name,
            series=_series(m),
            category=m.get("category", "") or "",
            event_ticker=m.get("event_ticker", "") or "",
            mutually_exclusive=bool(m.get("mutually_exclusive")),
        )
        for m in markets
        if lane_reject_reason(m, spec, s, now) is None
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


def ask_trend(candles: list[dict[str, Any]]) -> Trend | None:
    """Ask-price trend from Kalshi candlesticks. None when there is too little history."""
    asks: list[float] = []
    for c in candles:
        try:
            v = float((c.get("yes_ask") or {}).get("close_dollars") or 0.0)
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


def contracts_for_budget(budget_usd: float, ask: float) -> int:
    """How many contracts fit in the budget, counting the fee."""
    if ask <= 0:
        return 0
    n = int(budget_usd / (ask + fee_per_contract(ask)))
    while n > 0 and ask * n + order_fee(ask, n) > budget_usd:
        n -= 1
    return max(n, 0)
