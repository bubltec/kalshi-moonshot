"""The curve's own value of holding a position (2026-11-10, user: "i want to get rid of all the rigid exit ideas. it should be based on curve with spike and shock concepts built in"; enabled live on commodity_multiscale at the user's call).

A held 15-minute position is worth the chance its side settles at $1. The curve gives that chance from the spot's cushion over its reference and the time left, in the market's own variance: P_w = Phi(c / (r_w * sqrt(s))), c the signed cushion in log (positive = the bet is winning), s the seconds left, and r_w the rms of 60 s moves of the index over the last w seconds divided by sqrt(60) (the volatility at the horizon scale, not the one-second noise extrapolated). Two scales are read, the last 900 s and the last 180 s, and the pessimistic one (the lower P) is used. A spike raises the 180 s rate at once and pulls P toward even; a shock shrinks c the same way. A quiet market has both rates alike.
The exit (watcher._curve_hit) sells when the bid net of the sell fee is at least P plus the lane's margin, held for a few seconds. It takes profit and cuts losses with the same rule; nothing in it is a fixed number of basis points, a price level, or a market name.
Evidence: scripts/curve_exit_unified_replay_v2.py, scripts/curve_exit_shock_replay.py and studies/results/2026-11-10-curve-exit-shock-commodity-multiscale.txt: on 42 settled commodity_multiscale bets the 60 s scales at margin 0.06, 5 s persistence, any time left were +$4.28 against holding and +$7.03 against the lane's live exits, positive in both halves and all three curve shapes. The margin and scales were chosen after seeing the results, and the replay did not stack it on other exits, so on this lane it replaces them. On crypto the same rule did not beat the live shock exit (not enabled there)."""

from __future__ import annotations

import bisect
import math
from typing import NamedTuple

LONG, SHORT = 900, 180  # seconds the rate is read over
STEP = 60  # the horizon scale: the moves the rate is made of
NEED = LONG + STEP + 20  # seconds of history needed
STALE = 10.0  # the newest tick may be this old


class Curve(NamedTuple):
    p: float  # the chance the side pays, pessimistic over the two scales
    cushion: float  # signed log cushion over the reference
    rate_long: float  # per sqrt-second
    rate_short: float


def phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def read(ts: list[float], vs: list[float], now: float, ref: float, side: str, left: float) -> Curve | None:
    """The curve's value of holding at `now`, from the index ticks (ts ascending). None when the history is too short, a tick is stale, or the reference or time left is unusable."""
    if ref <= 0 or left <= 0 or not ts or len(ts) != len(vs):
        return None
    j = bisect.bisect_right(ts, now) - 1
    if j < 0 or now - ts[j] > STALE or ts[0] > now - NEED:
        return None
    n = LONG + STEP
    grid: list[float] = []
    for k in range(n + 1):
        i = bisect.bisect_right(ts, now - n + k) - 1
        if i < 0 or vs[i] <= 0:
            return None
        grid.append(math.log(vs[i]))
    inc = [(grid[k] - grid[k - STEP]) ** 2 / STEP for k in range(STEP, n + 1)]  # one entry per second, the last LONG of them
    r_long = math.sqrt(sum(inc[-LONG:]) / LONG)
    r_short = math.sqrt(sum(inc[-SHORT:]) / SHORT)
    sign = 1.0 if side == "yes" else -1.0
    c = (grid[-1] - math.log(ref)) * sign
    root = math.sqrt(left)
    ps = [phi(c / (r * root)) if r > 0 else (1.0 if c > 0 else 0.0) for r in (r_long, r_short)]
    return Curve(min(ps), c, r_long, r_short)
