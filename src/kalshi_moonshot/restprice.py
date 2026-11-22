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


def rest_price(spec: Any, side: str, ask: float, rows: Sequence[Sequence[float]], now: float) -> float:
    """The price a resting buy starts at."""
    return round(max(0.01, base_ask(spec, side, ask, rows, now) - spec.buy_rest_below), 4)
