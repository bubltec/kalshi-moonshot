import math
import random

from kalshi_moonshot import variants


def _walk(now, hours=7.0, drift_end=0.0, seed=1):
    rnd = random.Random(seed)
    n = int(hours * 3600)
    ts, vs, x = [], [], 0.0
    for i in range(n + 1):
        x += rnd.gauss(0, 1e-4) + (drift_end / 3600 if i > n - 3600 else 0.0)
        ts.append(now - n + i)
        vs.append(100.0 * math.exp(x))
    return ts, vs


def test_a_yes_bet_into_a_falling_hour_is_under_its_own_cutoff():
    now = 2_000_000.0
    ts, vs = _walk(now, drift_end=-0.02)
    got = variants.hour_drift_from(ts, vs, now, "yes", 0.25)
    assert got is not None
    cur, cut, n = got
    assert n >= variants.DRIFT_MIN_SAMPLES and cur < cut


def test_the_no_side_flips_the_sign():
    now = 2_000_000.0
    ts, vs = _walk(now, drift_end=-0.02)
    yes = variants.hour_drift_from(ts, vs, now, "yes", 0.25)
    no = variants.hour_drift_from(ts, vs, now, "no", 0.25)
    assert yes is not None and no is not None and no[0] > no[1] and abs(yes[0] + no[0]) < 1e-9


def test_short_history_or_stale_tick_is_unreadable():
    now = 2_000_000.0
    ts, vs = _walk(now, hours=3.0)
    assert variants.hour_drift_from(ts, vs, now, "yes", 0.25) is None
    ts, vs = _walk(now)
    assert variants.hour_drift_from(ts, vs, now + 60.0, "yes", 0.25) is None


def test_real_ticks_have_fractional_seconds():
    now = 2_000_000.0
    ts, vs = _walk(now, drift_end=-0.02)
    ts = [t + 0.37 for t in ts]  # the first tick is not on a whole second
    got = variants.hour_drift_from(ts, vs, now + 0.5, "yes", 0.25)
    assert got is not None and got[0] < got[1]
