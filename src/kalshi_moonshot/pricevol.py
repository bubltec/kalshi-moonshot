"""The price's one-second move over the NEXT stretch of a market, estimated four ways (2026-11-18, user: "as time expires the volatility rate increases"; "so having a static volatility number is pointless").

studies/results/2026-11-18-volatility-by-time-left.txt: the root-mean-square one-second change of Kalshi's mid is 1.9c at 7 to 10 minutes left, 4.8c at 1 to 2, 10.2c in the last 30 s. The exit's "typical minute" (quoteshock.typical_minute) is one trailing 15-minute number, then scaled by sqrt(time left): late in a market it is a calm average and the scaling shrinks it further. These functions are the candidates for a unit that rises on its own as the close nears; scripts/price_vol_estimators.py scores them held out.

Every function takes `seq`, the market's mid (the YES mid; a NO side's moves are the same) on a one-second grid, oldest first, the last element being now (grid() builds it from the quotes in memory), and `left`, seconds to the close at now. Each returns the estimated root-mean-square one-second change of the mid over the next stretch, in probability per second, or None when there is too little history to read. Pure: memory only, no coin, commodity or fixed price level. The constants are structural or stated, none tuned to the result.
"""

from __future__ import annotations

import bisect
import itertools
import math
from collections.abc import Sequence
from statistics import NormalDist

_N = NormalDist()
SPAN = 900  # seconds: the static reading's look back, as quoteshock.UNIT_SPAN
MIN_HISTORY = 300  # seconds of quotes before any reading, as quoteshock.UNIT_MIN_HISTORY
HORIZON = 30.0  # seconds: the stretch ahead the estimate is for
SHARE = 0.5  # (b) the trailing window is this share of the time left: a stated constant of the method (the window and the stretch it forecasts then shrink together), not tuned
FLOOR = 30  # (b) the window is never shorter than the stretch ahead (HORIZON): fewer one-second moves are mostly rounding
CLIP = 0.01  # (c) a mid is held this far inside 0 and 1 before it is turned into a cushion: a cent is the quote's own resolution
CAP = 0.5  # no estimate is over half a unit of probability a second: a bound, the price cannot move more than 1 in total


def grid(rows: list[tuple[float, float]], now: float, n: int = SPAN) -> list[float] | None:
    """The mid on a one-second grid for the last n seconds ending at `now`, carrying the last quote forward (as quoteshock.typical_minute). None when there are no quotes that old or the newest is over 10 s stale."""
    if not rows or rows[-1][0] < now - 10.0:
        return None
    ts = [r[0] for r in rows]
    n = int(min(n, now - ts[0] - 1.0))
    if n < MIN_HISTORY:
        return None
    idx = [bisect.bisect_right(ts, now - n + k) - 1 for k in range(n + 1)]
    if idx[0] < 0:
        return None
    return [float(rows[i][1]) for i in idx]  # plain floats: the bot's image has no numpy (2026-11-18, a numpy import here stopped the exit watcher from starting)


def _rms(seq: Sequence[float]) -> float | None:
    d = [b - a for a, b in itertools.pairwise(seq)]
    if not d:
        return None
    v = sum(x * x for x in d) / len(d)
    return math.sqrt(v) if v > 0 else None


def _bound(x: float | None) -> float | None:
    return None if x is None or not math.isfinite(x) or x <= 0 else min(x, CAP)


def static_rate(seq: Sequence[float], left: float = 0.0) -> float | None:
    """(a) The exit's unit as it is today: the root-mean-square one-second change over the last SPAN seconds (typical minute / sqrt(60)), carried to any horizon as it is, by sqrt(horizon) for a move and not at all for a per-second rate. Ignores the time left."""
    if len(seq) - 1 < MIN_HISTORY:
        return None
    return _bound(_rms(seq[-(SPAN + 1) :]))


def windowed_rate(seq: Sequence[float], left: float, share: float = SHARE, floor: int = FLOOR) -> float | None:
    """(b) The same trailing reading over a window that is `share` of the time left (never under `floor` seconds, never over SPAN), so it shortens toward the close and is read from the market's latest moves."""
    if len(seq) - 1 < MIN_HISTORY or left <= 0:
        return None
    w = int(min(max(share * left, floor), SPAN))
    return _bound(_rms(seq[-(w + 1) :]))


def implied_rate(seq: Sequence[float], left: float, horizon: float = HORIZON, span: int = SPAN) -> float | None:
    """(c) The model-implied one. The price is p = Phi(z), z = c / sqrt(time left), c the cushion the price implies in its own units. The cushion's one-second move has a rate r (the root-mean-square of its changes over the last `span` seconds: the market's own trailing spot-equivalent rate, read from the price because the archive holds no spot); then the price moves about phi(z) * r / sqrt(time left) a second, which rises on its own as the time runs out. Evaluated at the middle of the stretch ahead (time left less half the horizon, never under half of it)."""
    if len(seq) - 1 < MIN_HISTORY or left <= 0:
        return None
    s = seq[-(span + 1) :]
    n = len(s)
    z = [_N.inv_cdf(min(max(float(x), CLIP), 1.0 - CLIP)) for x in s]
    c = [zi * math.sqrt(left + (n - 1 - k)) for k, zi in enumerate(z)]  # the cushion at each grid second: z x sqrt(the time left then)
    r = _rms(c)
    if r is None:
        return None
    mid_left = max(left - horizon / 2.0, horizon / 2.0)
    phi = math.exp(-0.5 * z[-1] ** 2) / math.sqrt(2.0 * math.pi)
    return _bound(phi * r / math.sqrt(mid_left))


def mixed_rate(seq: Sequence[float], left: float) -> float | None:
    """(d) The geometric mean of (b) and (c): the latest raw moves and the model's own time sensitivity, each counting as a factor of the other, none picked."""
    b, c = windowed_rate(seq, left), implied_rate(seq, left)
    if b is None or c is None:
        return None
    return _bound(math.sqrt(b * c))


ESTIMATORS = {"static": static_rate, "windowed": windowed_rate, "implied": implied_rate, "mixed": mixed_rate}


def unit_of(rate: float | None) -> float | None:
    """A typical minute from a per-second rate: what the exit's rules read (quoteshock.typical_minute is sqrt(60) times the one-second rms)."""
    return None if rate is None else rate * math.sqrt(60.0)
