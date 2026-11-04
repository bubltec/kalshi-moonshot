"""What a taker buy would have got on the tape, calibrated against the lanes' real orders.

2026-11-04 calibration (1,170 live attempts since 11-02, decision price against the recorded quotes): an order is stamped within a few milliseconds of its decision, so the model takes the best ask of the order's own
side from the first quote at or after the decision (latency 0; waiting longer only made it worse: 0.4-0.7c of price error at 0, 1.2-2.9c at 2-4 s) and fills it when that ask is within the lane's limit (the decision
price plus its allowance). On the ordinary lanes (favorites, metals, energy, treasuries) that agrees with what happened 88-100% of the time with a price error of 0.4-0.9c. It does NOT work for the burst lanes that buy
a cheap side the instant a burst prints (the cheap side is gone before the quote tape shows it): there it called 61-70% of the attempts correctly with no depth rule and no latency that improved it. A paper twin of such a
lane is not to be trusted; its live record, and the record of the orders it could not fill (misses.py), are.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Fill:
    filled: bool
    price: float | None


def side_ask(bid: float, ask: float, side: str) -> float:
    return float(ask) if side == "yes" else 1.0 - float(bid)


def side_size(bid_size: float | None, ask_size: float | None, side: str) -> float:
    return float(ask_size or 0.0) if side == "yes" else float(bid_size or 0.0)


def simulate_taker_buy(quotes: Sequence[Sequence[float]], side: str, decision_ts: float, decision_ask: float, allowance: float, stake_usd: float = 0.0,
                       depth_ratio: float = 0.0, window: int = 0, latency: float = 0.0) -> Fill:
    """`quotes`: (time, YES bid, YES ask[, YES bid size, YES ask size]) ascending. The first quote at or after decision_ts + latency, and up to `window` quotes after it, is tried: the order fills at that side's ask when it
    is at most decision_ask + allowance and, with a depth_ratio, the book shows at least that share of the stake at the ask. No quote in time, or none inside the limit: not filled."""
    times = [q[0] for q in quotes]
    k0 = bisect.bisect_left(times, decision_ts + latency)
    for k in range(k0, min(len(quotes), k0 + 1 + window)):
        q = quotes[k]
        a = side_ask(q[1], q[2], side)
        if a > decision_ask + allowance + 1e-9:
            continue
        if depth_ratio > 0 and len(q) >= 5 and side_size(q[3], q[4], side) * a + 1e-9 < stake_usd * depth_ratio:
            continue
        return Fill(True, round(a, 4))
    return Fill(False, None)
