"""Pure selection logic: which markets are long shots worth a model's time, and what a bet costs.

Nothing here talks to the network, so it is all unit tested.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .config import Settings

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


def reject_reason(m: dict[str, Any], s: Settings, now: datetime) -> str | None:
    """None if the market qualifies as a tradable long shot, else why not."""
    skipped = {c.casefold() for c in s.skip_categories}
    category = (m.get("category") or "").casefold()
    if category and category in skipped:
        return "category skipped"
    if s.skip_numeric and is_numeric_threshold(m):
        return NUMERIC_REASON
    if m.get("market_type", "binary") != "binary":
        return "not binary"
    if m.get("status") not in {"active", "open"}:
        return "not active"
    ask, bid = _f(m, "yes_ask_dollars"), _f(m, "yes_bid_dollars")
    if not (s.min_price <= ask <= s.max_price):
        return "ask outside long-shot range"
    # Many long shots have an ask but no bid and no trades. Nothing can be sold into that.
    if bid <= 0.0:
        return "no bid (cannot exit)"
    if ask - bid > s.max_spread:
        return "spread too wide"
    if _f(m, "volume_fp") < s.min_volume:
        return "volume too low"
    hours = _hours_until(m.get("close_time", ""), now)
    if hours is None:
        return "no close time"
    if hours < s.min_hours_to_close:
        return "closes too soon"
    if hours > s.max_hours_to_close:
        return "closes too far out"
    return None


def long_shots(markets: list[dict[str, Any]], s: Settings, now: datetime | None = None):
    now = now or datetime.now(UTC)
    out: list[LongShot] = []
    for m in markets:
        if reject_reason(m, s, now) is not None:
            continue
        out.append(
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
                series=_series(m),
                category=m.get("category", "") or "",
            )
        )
    # Most traded first: those have the tightest, least stale prices.
    out.sort(key=lambda x: x.volume, reverse=True)
    return out


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
    """Cheap pre-filter for the steady lane. The price trend is checked afterwards."""
    skipped = {c.casefold() for c in s.skip_categories}
    category = (m.get("category") or "").casefold()
    if category and category in skipped:
        return "category skipped"
    if s.skip_numeric and is_numeric_threshold(m):
        return NUMERIC_REASON
    if m.get("market_type", "binary") != "binary":
        return "not binary"
    if m.get("status") not in {"active", "open"}:
        return "not active"
    ask, bid = _f(m, "yes_ask_dollars"), _f(m, "yes_bid_dollars")
    if not (s.steady_min_price <= ask <= s.steady_max_price):
        return "ask outside steady range"
    if bid <= 0.0:
        return "no bid (cannot exit)"
    if ask - bid > s.steady_max_spread:
        return "spread too wide"
    if _f(m, "volume_fp") < s.steady_min_volume:
        return "volume too low"
    hours = _hours_until(m.get("close_time", ""), now)
    if hours is None:
        return "no close time"
    if hours < s.steady_min_hours_to_close:
        return "closes too soon"
    if hours > s.steady_max_hours_to_close:
        return "closes too far out"
    return None


def steady_candidates(
    markets: list[dict[str, Any]], s: Settings, now: datetime | None = None
) -> list[LongShot]:
    now = now or datetime.now(UTC)
    out: list[LongShot] = []
    for m in markets:
        if steady_reject_reason(m, s, now) is not None:
            continue
        out.append(
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
                lane="steady",
                series=_series(m),
                category=m.get("category", "") or "",
            )
        )
    out.sort(key=lambda x: x.volume, reverse=True)
    return out


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


def trend_ok(t: Trend, s: Settings) -> bool:
    """Rising across the window and not rolling over right now."""
    return t.change >= s.steady_min_trend and t.last_step >= 0.0


def edge(model_p: float, ask: float) -> float:
    """Expected profit per $1 payout contract after the entry fee."""
    return model_p - ask - fee_per_contract(ask)


def contracts_for_budget(budget_usd: float, ask: float) -> int:
    """How many contracts fit in the budget, counting the fee."""
    if ask <= 0:
        return 0
    n = int(budget_usd / (ask + fee_per_contract(ask)))
    while n > 0 and ask * n + order_fee(ask, n) > budget_usd:
        n -= 1
    return max(n, 0)
