"""quoteshock.typical_minute, the reading the swing hold uses (2026-11-14): sqrt(60) times the root-mean-square one-second change of a market's mid over the last 15 minutes."""

import math

from kalshi_moonshot import quoteshock

NOW = 10_000.0


def path(step, seconds=900, start=0.5, flip=True):
    """A mid that moves by `step` every second, alternating up and down (or one way)."""
    rows, mid = [], start
    for i in range(seconds + 1):
        rows.append((NOW - seconds + i, mid))
        mid += step if (i % 2 == 0 or not flip) else -step
    return rows


def test_a_mid_that_moves_one_cent_every_second_has_a_known_typical_minute():
    got = quoteshock.typical_minute(path(0.01), NOW)
    assert abs(got - 0.01 * math.sqrt(60)) < 1e-9


def test_a_larger_step_gives_a_proportionally_larger_unit():
    a, b = quoteshock.typical_minute(path(0.01), NOW), quoteshock.typical_minute(path(0.03), NOW)
    assert abs(b / a - 3.0) < 1e-9


def test_a_flat_mid_stale_mids_and_a_short_history_have_no_unit():
    assert quoteshock.typical_minute([(NOW - 900 + i, 0.5) for i in range(901)], NOW) is None  # flat
    assert quoteshock.typical_minute(path(0.01), NOW + 60) is None  # the newest mid is a minute old
    assert quoteshock.typical_minute(path(0.01, seconds=200), NOW) is None  # under five minutes of quotes
    assert quoteshock.typical_minute([], NOW) is None
