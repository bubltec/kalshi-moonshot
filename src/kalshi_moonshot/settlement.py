"""The final minute of a 15-minute crypto market, computed exactly.

Kalshi settles these markets on the AVERAGE of the settlement index over the last 60 seconds, not on the last tick. So in the last minute part of the answer is already locked in, the rest depends on the spot over the seconds that remain,
and a smoothed chart of the spot hides how fast it can move. With T the close, s the seconds left, X the spot now and sigma its per-second volatility (a log return), the average A the market settles on is, under a random walk:
  s >= 60  the average has not started: mean X, variance (sigma X)^2 (s - 60 + 20)
  s <  60  a part is locked: mean ((60 - s) x the average of the spot over the seconds already past + s X) / 60, variance (sigma X)^2 s^3 / (3 x 60^2)
           (the weights are the exact elapsed and remaining seconds, which always add to 60: counting whole-second ticks instead is off by one tick whenever s has a fraction, and one tick is 1/60 of the price, many times the standard deviation)
and the chance the market finishes above the target (the strike) is Phi((mean - strike) / sd).
scripts/settlement_average_study.py compares this with Kalshi's own price over the last 150 seconds, and scripts/cashout_flip_study.py replays the cash-out and flip rules built on it. Nothing here places an order.
"""

from __future__ import annotations

import bisect
import itertools
import logging
import math
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger("moonshot")

WINDOW = 60.0  # the seconds the settlement average covers


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def chance_above(strike: float, spot: float, locked_avg: float, seconds_left: float, sigma_s: float) -> float | None:
    """The chance the 60-second average at the close ends above `strike`. `locked_avg` is the average of the spot over the part of the average that is already past (ignored while seconds_left >= 60);
    `sigma_s` is the spot's per-second volatility as a log return. None when the inputs cannot give an answer."""
    if spot <= 0 or strike <= 0 or seconds_left <= 0 or sigma_s <= 0:
        return None
    sd1 = sigma_s * spot
    if seconds_left >= WINDOW:
        mean, var = spot, sd1 ** 2 * (seconds_left - WINDOW + WINDOW / 3.0)
    else:
        if locked_avg <= 0:
            return None
        elapsed = WINDOW - seconds_left
        mean, var = (elapsed * locked_avg + seconds_left * spot) / WINDOW, sd1 ** 2 * seconds_left ** 3 / (3.0 * WINDOW ** 2)
    return phi((mean - strike) / math.sqrt(max(var, 1e-12)))


def sigma_per_second(ts: list[float], values: list[float], now: float, minutes: float = 30.0) -> float | None:
    """Per-second volatility of the log spot from its moves over 10-second steps in the last `minutes` minutes (the index ticks come about once a second but not evenly). None with too little history."""
    if not ts:
        return None
    samples = []
    t = now - minutes * 60.0
    while t <= now:
        i = bisect.bisect_right(ts, t) - 1
        if i >= 0 and t - ts[i] < 20.0:
            samples.append(values[i])
        t += 10.0
    if len(samples) < 60:
        return None
    rets = [math.log(b / a) for a, b in itertools.pairwise(samples) if a > 0 and b > 0]
    return math.sqrt(sum(r * r for r in rets) / len(rets)) / math.sqrt(10.0) if rets else None


def locked_average(ts: list[float], values: list[float], close_ts: float, now: float) -> float | None:
    """The average of the spot over the part of the settlement average that has already passed (from close - 60 s up to now), each whole second taken as the last tick at or before it. None if there is no such second or one has no recent tick."""
    total, n = 0.0, 0
    u = close_ts - WINDOW
    while u < now - 0.5 and u < close_ts:
        i = bisect.bisect_right(ts, u) - 1
        if i < 0 or u - ts[i] > 20.0:
            return None
        total += values[i]
        n += 1
        u += 1.0
    return total / n if n else None


class Model:
    """The live version of the settlement chance, shared by everything that needs it: it keeps the latest index tick of each settlement index (fed from the socket), reads the last 30 minutes of ticks from the tape for the volatility (cached for a minute)
    and the locked part of the average, and answers `chance_yes`. `tape_factory` opens a read connection to the tape (one per thread)."""

    def __init__(self, tape_factory: Callable[[], Any] | None = None):
        self.tape_factory = tape_factory
        self.spot: dict[str, tuple[float, float]] = {}  # index id -> (time, latest value)
        self.sigma: dict[str, tuple[float, float | None]] = {}  # index id -> (when computed, per-second volatility)
        self._tls = threading.local()

    def note_spot(self, index_id: str, ts: float, value: float) -> None:
        self.spot[index_id] = (ts, value)

    def _tape(self) -> Any:
        if getattr(self._tls, "tape", None) is None and self.tape_factory is not None:
            self._tls.tape = self.tape_factory()
        return getattr(self._tls, "tape", None)

    def chance_yes(self, index_id: str, strike: float, close_ts: float, now: float) -> float | None:
        """The chance the market finishes above `strike`, or None when anything it needs is missing or stale (a spot tick older than 5 s, no history, no tape)."""
        left = close_ts - now
        spot = self.spot.get(index_id)
        if left <= 0 or spot is None or now - spot[0] > 5.0:
            return None
        try:
            tape = self._tape()
            if tape is None:
                return None
            cached = self.sigma.get(index_id)
            if cached is None or now - cached[0] > 60.0:
                rows = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value > 0 ORDER BY ts", (index_id, now - 1860.0, now)).fetchall()
                cached = (now, sigma_per_second([r[0] for r in rows], [r[1] for r in rows], now))
                self.sigma[index_id] = cached
            if cached[1] is None:
                return None
            locked = 0.0
            if left < WINDOW:
                rows = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value > 0 ORDER BY ts", (index_id, close_ts - WINDOW - 25.0, now)).fetchall()
                got = locked_average([r[0] for r in rows], [r[1] for r in rows], close_ts, now)
                if got is None:
                    return None
                locked = got
            return chance_above(strike, spot[1], locked, left, cached[1])
        except Exception:
            log.debug("settlement model: could not compute for %s", index_id, exc_info=True)
            self._tls.tape = None
            return None
