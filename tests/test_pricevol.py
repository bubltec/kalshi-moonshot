"""pricevol: four estimates of the price's one-second move over the next stretch (2026-11-18, user: "as time expires the volatility rate increases"). Unit tests on synthetic markets; the held-out test is scripts/price_vol_estimators.py."""
import math
from statistics import NormalDist

import numpy as np
import pytest

from kalshi_moonshot import pricevol

N = NormalDist()


def market(rate=0.02, seconds=900, seed=1):
    """A synthetic market whose price is Phi(c / sqrt(time left)) with c a random walk of `rate` a second: the price's own vol rises toward the close. Returns the mid on a one-second grid, 900 s to the close."""
    rng = np.random.default_rng(seed)
    c = np.cumsum(rng.normal(0.0, rate, seconds + 1)) + 0.3
    lefts = np.arange(seconds, -1, -1, dtype=float) + 1.0
    return np.array([N.cdf(x / math.sqrt(l)) for x, l in zip(c, lefts)])


def at(seq, left):
    """The history up to the mark with `left` seconds to the close of the 900 s market."""
    return seq[: 900 - left + 1]


def test_unit_the_static_reading_does_not_depend_on_the_time_left_and_the_timed_ones_rise_toward_the_close():
    seq = market()
    early, late = at(seq, 600), at(seq, 120)
    assert pricevol.static_rate(early, 600) == pricevol.static_rate(early, 120)  # it reads no clock
    timed = pricevol.implied_rate(late, 120) / pricevol.implied_rate(early, 600)
    static = pricevol.static_rate(late, 120) / pricevol.static_rate(early, 600)
    assert timed > 1.5 and timed > static  # the model-implied reading follows sqrt(600/120) = 2.2; the 15-minute average barely moves with it
    assert pricevol.windowed_rate(late, 120) is not None and pricevol.mixed_rate(late, 120) is not None


def test_unit_the_windowed_reading_shortens_with_the_time_left():
    quiet = np.full(901, 0.5)
    quiet[::2] += 0.001  # a flicker of a tenth of a cent for 800 seconds
    burst = quiet.copy()
    burst[-41:] = 0.5 + 0.05 * (np.arange(41) % 2)  # then 40 s of five-cent moves
    assert pricevol.windowed_rate(burst, 80) > 4 * pricevol.static_rate(burst, 80)  # half of 80 s left = the last 40 s
    assert pricevol.windowed_rate(burst, 800) < pricevol.static_rate(burst, 800) * 2  # with lots of time left the window is the whole history


def test_unit_estimates_are_bounded_and_finite_at_the_close():
    seq = market(rate=5.0)  # a wild cushion: the price lives at the walls
    for left in (900 - 300, 300, 60, 5, 1, 0.5):
        for name, fn in pricevol.ESTIMATORS.items():
            got = fn(seq[: 900 - int(left) + 1] if left >= 1 else seq, left)
            assert got is None or (0.0 < got <= pricevol.CAP and math.isfinite(got)), (name, left, got)


def test_unit_none_with_too_little_history_a_flat_market_or_no_time_left():
    short = market()[:200]
    for fn in pricevol.ESTIMATORS.values():
        assert fn(short, 600) is None
        assert fn(np.full(901, 0.5), 300) is None  # never moved
    assert pricevol.windowed_rate(market()[:600], 0) is None and pricevol.implied_rate(market()[:600], -1) is None


def test_unit_a_no_side_moves_as_its_yes_side():
    seq = market()
    for fn in (pricevol.windowed_rate, pricevol.implied_rate):
        assert fn(1.0 - seq[:700], 200) == pytest.approx(fn(seq[:700], 200))


def test_unit_unit_of_scales_by_sqrt_60_and_passes_none():
    assert pricevol.unit_of(0.01) == pytest.approx(0.01 * math.sqrt(60.0)) and pricevol.unit_of(None) is None


def test_unit_grid_carries_the_last_quote_forward_and_refuses_stale_or_short_history():
    rows = [(1000.0 + 10 * k, 0.5 + 0.001 * k) for k in range(80)]  # a quote every ten seconds for 790 s
    got = pricevol.grid(rows, 1790.0)
    assert got is not None and len(got) == 790 and got[-1] == rows[-1][1] and got[0] == rows[0][1]
    assert pricevol.grid(rows, 1850.0) is None  # the newest quote is a minute old
    assert pricevol.grid(rows[:20], 1190.0) is None  # under 300 s of quotes
    assert pricevol.grid([], 1000.0) is None
