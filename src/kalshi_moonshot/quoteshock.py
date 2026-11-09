"""The shock exit's reading when a market has no spot feed (2026-11-09, user: "implement the fix for shock exit to work with treasury ... modular and adaptable for any lane").

The spot shock exit (watcher._shock_hit) reads an index: how far it moved against the bet in shock_seconds, how far it sits from the strike, and the market's typical minute. A market with no index (the Treasury-yield markets settle on a Pyth feed we cannot read) still has its own price, and the same three readings exist on it: the held side's mid is the "spot", 0.50 is the "strike" (the side is ahead above it), and the typical minute is the root-mean-square one-second move of the mid over the last 15 minutes times sqrt(60), the same definition variants.unit_index uses on an index.

This module is pure: it takes the mid history (ticks.mids.rows) and returns the three readings in per-mille of probability (1 cent = 10), so the watcher applies the same thresholds it applies to basis points. Lane field `shock_source = "quote"` selects it; the default "index" keeps the spot path. Nothing here knows a series or an asset: any lane with shock_units can choose either source.
"""

from __future__ import annotations

import bisect
import itertools
import math
from typing import NamedTuple

SCALE = 1000.0  # per-mille of probability: 1 cent is 10 units. A quote moves in whole cents, so a one-cent step is a visible 10.
UNIT_SPAN = 900.0  # the typical minute is read over the last 15 minutes, as for an index
UNIT_MIN_HISTORY = 300.0  # ...and not from under 5 minutes of quotes (no assumption)
FRESH = 10.0  # a mid older than this is not a reading


class Reading(NamedTuple):
    ts: float  # when the newest mid was quoted
    margin: float  # the held side's mid less 0.50, in per-mille (positive = ahead)
    move: float  # the held side's mid now less its mid `seconds` ago, in per-mille (negative = against the bet)
    unit: float  # the typical minute of the mid, in per-mille


def held(mid: float, side: str) -> float:
    """The mid of the side we hold: a YES mid as it is, a NO mid as one less."""
    return mid if side == "yes" else 1.0 - mid


def typical_minute(rows: list[tuple[float, float]], now: float, span: float = UNIT_SPAN) -> float | None:
    """sqrt(60) times the root-mean-square one-second change of the mid over the last `span` seconds, from the quote mids on a 1 s grid that carries the last quote forward, in probability. None when the history is under UNIT_MIN_HISTORY seconds, the newest quote is stale, or the mid never moved."""
    if not rows:
        return None
    ts = [r[0] for r in rows]
    if ts[-1] < now - FRESH or ts[0] > now - UNIT_MIN_HISTORY:
        return None
    n = int(min(span, now - ts[0] - 1.0))
    if n < UNIT_MIN_HISTORY:
        return None
    seq = []
    for k in range(n + 1):
        i = bisect.bisect_right(ts, now - n + k) - 1
        if i < 0:
            return None
        seq.append(rows[i][1])
    sq = sum((b - a) ** 2 for a, b in itertools.pairwise(seq))
    return math.sqrt(sq / n) * math.sqrt(60.0) if sq > 0 else None


def read(rows: list[tuple[float, float]], side: str, now: float, seconds: float) -> Reading | str:
    """The three readings for a bet on `side`, or the reason there is none (a string, for the watch note)."""
    if not rows:
        return "no quote mids in memory"
    ts_now, mid_now = rows[-1]
    if now - ts_now > FRESH:
        return f"the newest mid is {now - ts_now:.0f}s old"
    then = now - seconds
    if rows[0][0] > then:
        return f"{now - rows[0][0]:.0f}s of mids, under the {seconds:.0f}s look-back"
    i = bisect.bisect_right([r[0] for r in rows], then) - 1
    past = rows[i][1]
    unit = typical_minute(rows, now)
    if unit is None:
        return "the typical minute of the mid cannot be read (history under 300 s, stale, or flat)"
    return Reading(ts_now, (held(mid_now, side) - 0.5) * SCALE, (held(mid_now, side) - held(past, side)) * SCALE, unit * SCALE)
