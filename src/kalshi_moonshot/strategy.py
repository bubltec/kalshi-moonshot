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


def reject_reason(m: dict[str, Any], s: Settings, now: datetime) -> str | None:
    """None if the market qualifies as a tradable long shot, else why not."""
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
            )
        )
    # Most traded first: those have the tightest, least stale prices.
    out.sort(key=lambda x: x.volume, reverse=True)
    return out


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
