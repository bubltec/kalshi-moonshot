"""The hour view's movement read at several scales together (2026-11-15, user: "the hour view should look further, but we need the context of the current hour ... in 15, 30, 60, 120, 180 mins")."""
import math
import random

import pytest

from kalshi_moonshot import curverisk


def series(minutes, seed=1, sigma=0.0004, wild_from=None):
    """One tick a minute for `minutes` minutes ending at 0, a random walk whose one-minute move is `sigma`, 5x as large for the minutes before `wild_from` minutes ago when given."""
    rng = random.Random(seed)
    ts, vs, lv = [], [], math.log(100.0)
    for k in range(minutes, -1, -1):
        s = sigma * (5.0 if wild_from is not None and k > wild_from else 1.0)
        lv += rng.gauss(0, s)
        ts.append(-60.0 * k)
        vs.append(math.exp(lv))
    return ts, vs


def test_unit_one_scale_of_sixty_is_the_hour_rate():
    ts, vs = series(240)
    assert curverisk.multiscale_rate(ts, vs, 0.0, (60,)) == pytest.approx(curverisk.hour_rate(ts, vs, 0.0, 1.0))


def test_unit_several_scales_are_the_geometric_mean_of_each_scales_rate():
    ts, vs = series(240, wild_from=70)  # the older hours were five times as wild as the last hour
    r = {m: curverisk.multiscale_rate(ts, vs, 0.0, (m,)) for m in (15, 30, 60, 120, 180)}
    got = curverisk.multiscale_rate(ts, vs, 0.0, (15, 30, 60, 120, 180))
    assert got == pytest.approx(math.exp(sum(math.log(x) for x in r.values()) / 5))
    assert r[15] < r[120] < r[180]  # the longer scales carry the wild earlier hours
    assert r[60] < got < r[180]  # the blend sits between the current hour and the longest reach


def test_unit_a_scale_with_too_few_one_minute_moves_is_left_out_not_guessed():
    ts, vs = series(25)  # 25 minutes: the 30, 60, 120 and 180 minute scales have under 30 moves; the 15 minute scale has its 15
    assert curverisk.multiscale_rate(ts, vs, 0.0, (15, 30, 60, 120, 180)) == pytest.approx(curverisk.multiscale_rate(ts, vs, 0.0, (15,)))


def test_unit_a_longer_scale_with_part_of_its_history_reads_what_it_has():
    ts, vs = series(100)  # the 120 and 180 minute scales hold 100 moves each: enough (30 or more) to read, over what is there
    part = curverisk.multiscale_rate(ts, vs, 0.0, (120,))
    assert part == pytest.approx(curverisk.multiscale_rate(ts, vs, 0.0, (100,)))


def test_unit_no_history_or_no_scale_gives_none():
    assert curverisk.multiscale_rate([], [], 0.0, (60,)) is None
    ts, vs = series(10)
    assert curverisk.multiscale_rate(ts, vs, 0.0, (60,)) is None  # under 30 one-minute moves
    ts, vs = series(240)
    assert curverisk.multiscale_rate(ts, vs, 0.0, ()) is None


def test_unit_long_chance_with_scales_uses_that_rate_and_without_them_the_hour():
    ts, vs = series(240, wild_from=70)
    ref = vs[-61]
    base = curverisk.long_chance(ts, vs, 0.0, ref, "yes", 300.0)
    assert base == pytest.approx(curverisk.long_chance(ts, vs, 0.0, ref, "yes", 300.0, scales=()))
    rate = curverisk.multiscale_rate(ts, vs, 0.0, (15, 30, 60, 120, 180))
    cush = math.log(vs[-1]) - math.log(ref)
    from statistics import NormalDist

    assert curverisk.long_chance(ts, vs, 0.0, ref, "yes", 300.0, scales=(15, 30, 60, 120, 180)) == pytest.approx(NormalDist().cdf(cush / (rate * math.sqrt(300.0))))
    assert curverisk.long_chance(ts, vs, 0.0, ref, "no", 300.0, scales=(15, 30, 60, 120, 180)) == pytest.approx(1.0 - curverisk.long_chance(ts, vs, 0.0, ref, "yes", 300.0, scales=(15, 30, 60, 120, 180)))
