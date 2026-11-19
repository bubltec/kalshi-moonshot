import math
import random

from kalshi_moonshot import config, curvemodel
from kalshi_moonshot.estimators import Estimate


class Fixed:
    def estimate(self, client, settings, shot):
        return Estimate(0.9, "fixed")


def _ticks(now, n=4000, drift=0.0, seed=1):
    random.seed(seed)
    p = 100.0
    ts, vs = [], []
    for i in range(n + 1):
        p *= math.exp(random.gauss(drift, 1e-4))
        ts.append(now - n + i)
        vs.append(p)
    return ts, vs


def test_the_readings_are_nine_slopes_and_nine_variabilities_and_flip_with_the_side():
    now = 1_900_000_000.0
    ts, vs = _ticks(now)
    g, sq, k = curvemodel.live_grid(ts, vs, now)
    yes = curvemodel.features(g, sq, k, math.log(vs[-1] * 0.999), 1)
    no = curvemodel.features(g, sq, k, math.log(vs[-1] * 0.999), -1)
    assert len(yes) == 18 and len(no) == 18
    for j in range(0, 18, 2):
        assert abs(yes[j] + no[j]) < 1e-9  # the same curve read from the other side: the slope flips sign
        assert yes[j + 1] > 0 and abs(yes[j + 1] - no[j + 1]) < 1e-9  # the variability does not
    assert curvemodel.live_grid(ts[-1000:], vs[-1000:], now) is None  # under 55 minutes of history: unreadable, never a guess


def test_the_fit_learns_a_planted_slope_and_the_bar_is_a_quantile_of_the_training_moments():
    random.seed(3)
    rows = []
    for _ in range(4000):
        read = [random.gauss(0, 1) for _ in range(18)]
        ask = random.uniform(0.2, 0.7)
        rows.append((read, ask, 0.3 * read[6] + random.gauss(0, 0.5)))  # the 5-minute slope (index 6) pays
    model = curvemodel.fit(rows)
    assert model is not None and curvemodel.fit(rows[:100]) is None
    hi = curvemodel.predict(model, [0.0] * 6 + [2.0] + [0.0] * 11, 0.4)
    lo = curvemodel.predict(model, [0.0] * 6 + [-2.0] + [0.0] * 11, 0.4)
    assert hi > lo + 0.8
    assert model["cuts"]["0.05"] > model["cuts"]["0.2"] > model["cuts"]["0.5"]


def test_the_time_left_readings_shrink_a_cushion_that_has_more_time_to_be_lost():
    now = 1_900_000_000.0
    ts, vs = _ticks(now)
    g, sq, k = curvemodel.live_grid(ts, vs, now)
    strike = math.log(vs[-1] * 0.999)  # the spot sits 0.1% over the strike on the yes side
    far = curvemodel.features(g, sq, k, strike, 1, None, 800.0)
    near = curvemodel.features(g, sq, k, strike, 1, None, 60.0)
    no = curvemodel.features(g, sq, k, strike, -1, None, 60.0)
    assert len(far) == len(near) == 18 + len(curvemodel.TIME_NAMES)
    assert len(curvemodel.features(g, sq, k, strike, 1)) == 18  # without the seconds left, only the 18
    assert far[18] == 800.0 / 60.0 and near[18] == 1.0
    z_far, z_near = far[19], near[19]
    assert z_near > z_far > 0  # the same cushion is worth more with less time left to lose it in
    win_far, win_near, win_no = far[27], near[27], no[27]
    assert win_near > win_far > 0.5 > win_no  # the chance it holds to the close rises as the time runs down; the other side is the mirror


def test_a_model_fitted_with_the_time_readings_names_them_and_one_without_keeps_the_old_names():
    random.seed(5)
    n_new = 18 + len(curvemodel.TIME_NAMES)
    rows = [([random.gauss(0, 1) for _ in range(n_new)], random.uniform(0.2, 0.7), random.gauss(0, 0.5)) for _ in range(3100)]
    model = curvemodel.fit(rows)
    assert model["names"][18:31] == curvemodel.TIME_NAMES and len(model["mu"]) == n_new + 1
    old = curvemodel.fit([(r[0][:18], r[1], r[2]) for r in rows])
    assert old["names"] == curvemodel.NAMES


def _ramp(now, n=4000):
    """1 s ticks: a little noise, then the last 13 minutes accelerate upward (a convex path)."""
    random.seed(2)
    ts, vs = [], []
    p = 100.0
    for i in range(n + 1):
        tau = max(0, i - (n - 780))
        p *= math.exp(random.gauss(0, 1e-5) + 2e-8 * tau)
        ts.append(now - n + i)
        vs.append(p)
    return ts, vs


def test_the_curvature_readings_are_level_slope_and_curvature_per_window_and_flip_with_the_side():
    now = 1_900_000_000.0
    ts, vs = _ramp(now)
    g, sq, k = curvemodel.live_grid(ts, vs, now)
    strike = math.log(100.0)
    yes = curvemodel.features_curvature(g, sq, k, strike, 1)
    no = curvemodel.features_curvature(g, sq, k, strike, -1)
    assert len(yes) == 9 == len(curvemodel.QUAD_NAMES)
    assert all(abs(a + b) < 1e-9 for a, b in zip(yes, no, strict=True))  # the same curve read from the other side
    level13, slope13, curv13 = yes[3:6]
    assert level13 > 0 and slope13 > 0 and curv13 > 0  # above the strike, rising, and accelerating
    assert curvemodel.features_curvature(g[: 34 * 60 - 5], sq[: 34 * 60 - 5], 34 * 60 - 6, strike, 1) is None  # a window the history does not cover: no reading, never a guess


def test_the_buy_bar_for_a_share_between_the_stored_quantiles_is_interpolated():
    model = {"cuts": {"0.05": 0.09, "0.1": 0.06, "0.2": 0.02, "0.3": -0.006, "0.5": -0.047}}
    assert curvemodel.cut_for(model, 0.10) == 0.06 and curvemodel.cut_for(model, 0.2) == 0.02  # a stored quantile is used as it is
    assert abs(curvemodel.cut_for(model, 0.15) - 0.04) < 1e-9  # halfway between the 0.10 and 0.20 bars
    assert curvemodel.cut_for(model, 0.01) == 0.09 and curvemodel.cut_for(model, 0.9) == -0.047  # clamped at the ends


def test_a_paced_job_sleeps_for_the_share_of_time_it_may_not_use(monkeypatch):
    naps = []
    clock = [100.0]
    monkeypatch.setattr(curvemodel.time, "sleep", naps.append)
    monkeypatch.setattr(curvemodel.time, "monotonic", lambda: clock[0])
    pace = curvemodel.Pacer(0.25)
    clock[0] += 2.0  # two seconds of work
    pace.tick()
    assert naps == [6.0]  # a quarter of the time on the CPU: three times the work asleep
    clock[0] += 1000.0
    pace.tick()
    assert naps[-1] == curvemodel.PACE_MAX_PAUSE  # never one long sleep
    for duty in (1.0, 0.0):
        before = len(naps)
        clock[0] += 5.0
        curvemodel.Pacer(duty).tick()
        assert len(naps) == before  # full speed: no sleep


def test_the_daily_refit_is_paced_and_a_study_is_not(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import inspect

    default = inspect.signature(curvemodel.build_rows).parameters["duty"].default
    seen = {}
    monkeypatch.setattr(config, "reload", config.load)  # main() reloads the settings: do not leave this test's data dir in the shared settings cache
    monkeypatch.setattr(curvemodel, "build_rows", lambda s, **kw: seen.update(kw) or [])
    curvemodel.main()
    assert seen["duty"] == curvemodel.PACE_DUTY < 1.0
    assert default == 1.0


def test_curvature2_splits_the_bend_by_sign_and_damps_the_cushion_by_motion():
    read = [1.0, 0.5, 2.0, 0.8, -1.0, -3.0, 0.2, 0.0, 0.0]  # per window: level, slope, curvature
    out = curvemodel.expand_curvature2(read)
    assert len(out) == len(curvemodel.CURV2_NAMES) == 13
    assert out[:4] == [1.0, 0.5, 2.0, 0.0]  # 5 min: bends toward the bet's side, nothing away
    assert out[4:8] == [0.8, -1.0, 0.0, -3.0]  # 13 min: bends away
    assert out[8:12] == [0.2, 0.0, 0.0, 0.0]
    assert abs(out[12] - 0.8 / (1 + 1.0 + 3.0)) < 1e-12  # sat_13m: the level over one plus the slope and bend magnitudes
    flipped = curvemodel.expand_curvature2([-x for x in read])
    assert flipped[2] == 0.0 and flipped[3] == -2.0  # the other side of the same curve swaps which sign each bend lands in
    assert curvemodel.lookback_seconds("curvature2") == 34 * 60
    model = curvemodel.fit([(curvemodel.expand_curvature2([random.gauss(0, 1) for _ in range(9)]), 0.8, random.gauss(0, 1)) for _ in range(3100)], kind="curvature2")
    assert model["names"] == [*curvemodel.CURV2_NAMES, "ask"] and len(model["mu"]) == 14


def test_the_three_hour_window_is_a_reading_that_needs_three_hours_of_history():
    now = 1_900_000_000.0
    ts = [now - 4 * 3600 + i for i in range(4 * 3600 + 1)]
    vs = [100.0 + 0.0001 * i + 0.01 * ((i // 7) % 2) for i in range(len(ts))]
    g, sq, k = curvemodel.live_grid(ts, vs, now, 180 * 60)
    strike = math.log(99.0)
    read = curvemodel.readings_for("curvature3", g, sq, k, strike, 1)
    assert read is not None and len(read) == 12 == len(curvemodel.LONG_NAMES)
    assert read[:9] == curvemodel.readings_for("curvature", g, sq, k, strike, 1)  # the first three windows read the same
    assert curvemodel.lookback_seconds("curvature3") == 180 * 60
    short = [ts[0] + 2 * 3600 + i for i in range(2 * 3600 + 1)]
    got = curvemodel.live_grid(short, [100.0 + 0.01 * (i % 5) for i in range(len(short))], short[-1], 180 * 60)
    why: list[str] = []
    assert got is None or curvemodel.readings_for("curvature3", got[0], got[1], got[2], strike, 1, why) is None  # two hours of history cannot read a three-hour window
