"""Where a resting buy starts (lane fields buy_rest_below and buy_rest_avg_seconds), in one place so the live order and the paper judge agree.

Without buy_rest_avg_seconds the order starts `buy_rest_below` under the ask at the decision. With it (2026-11-22, user, on the wave-top lane: "when it's peaking it's going to flutter up and down ... you could take an average of like 5s") it starts `buy_rest_below` under the LOWER of the ask and the average ask of the last buy_rest_avg_seconds seconds, so a decision that lands on an up-tick does not start the order high, and a down-tick is never raised to the average (the order is always under the ask). The side's ask is the YES ask for YES and one minus the YES bid for NO. The rows are (time, YES bid, YES ask) as the quote memory and the tape hold them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def side_ask(bid: float, ask: float, side: str) -> float | None:
    """The side's ask from one YES quote, or None when the quote has no price for that side."""
    if side == "yes":
        return ask if ask > 0 else None
    return 1.0 - bid if bid > 0 else None


def average_ask(rows: Sequence[Sequence[float]], side: str, now: float, seconds: float) -> float | None:
    """The mean ask of the side over the quotes in the last `seconds` up to `now`; None with no usable quote."""
    asks = [a for r in rows if now - seconds <= r[0] <= now and (a := side_ask(r[1], r[2], side)) is not None]
    return sum(asks) / len(asks) if asks else None


def base_ask(spec: Any, side: str, ask: float, rows: Sequence[Sequence[float]], now: float) -> float:
    """The ask the order rests under: the decision's ask, or the lower of it and the recent average when the lane sets buy_rest_avg_seconds."""
    seconds = float(getattr(spec, "buy_rest_avg_seconds", 0.0) or 0.0)
    if seconds <= 0:
        return ask
    avg = average_ask(rows, side, now, seconds)
    return ask if avg is None else min(ask, avg)


def rest_wait(spec: Any, left: float) -> float:
    """How long a resting buy rests: buy_rest_wait_share of the seconds left to the close, never under buy_rest_seconds; with no share (or no time left read) buy_rest_seconds."""
    seconds = float(spec.buy_rest_seconds)
    share = float(getattr(spec, "buy_rest_wait_share", 0.0) or 0.0)
    return max(seconds, share * left) if share > 0 and left > 0 else seconds


def side_bid(bid: float, ask: float, side: str) -> float | None:
    """The side's bid from one YES quote (a NO bid is 1 minus the YES ask), or None when the quote has no price for it."""
    if side == "yes":
        return bid if bid > 0 else None
    return 1.0 - ask if 0 < ask < 1 else None


def floor_of(spec: Any) -> float:
    """The lowest price the lane buys at: the low end of its `price` range (0 for a spec without one)."""
    band = getattr(spec, "price", None)
    try:
        return float(band[0]) if band else 0.0
    except (TypeError, ValueError, IndexError):
        return 0.0


def in_band(spec: Any, price: float, ask: float | None = None) -> float:
    """A resting buy's price, never under the lane's floor (2026-11-27, user, on the wave lane's live bets: "not betting in the price range"): the order rests under the ask, so with the ask a cent or two over the floor it sat UNDER the floor, and
    12 of the lane's 145 live buys of 01:30 to 07:15 ET filled at 0.14 to 0.17 on a lane whose range starts at 0.18. The floor is the lane's own; a price at or over the ask is the caller's to refuse (no resting buy fits between the floor and the ask)."""
    lo = floor_of(spec)
    if ask is not None and lo >= ask - 1e-9:
        return price  # the ask itself is at or under the floor: the lane's price step and the floor check on the ask decide that, not this price
    return max(price, lo)


def rest_price(spec: Any, side: str, ask: float, rows: Sequence[Sequence[float]], now: float, bid: float | None = None) -> float:
    """The price a resting buy starts at: the side's bid when the lane sets buy_rest_at_bid and a bid is known, else under the ask; never under the lane's price floor (in_band)."""
    if getattr(spec, "buy_rest_at_bid", False) and bid is not None and bid > 0:
        return round(in_band(spec, max(0.01, bid), ask), 4)
    return round(in_band(spec, max(0.01, base_ask(spec, side, ask, rows, now) - spec.buy_rest_below), ask), 4)
