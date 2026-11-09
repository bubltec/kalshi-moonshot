import json
import math
import random
import time

from kalshi_moonshot import config, curvemodel, regime, runner, ticks, variants
from kalshi_moonshot.estimators import Estimate
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot


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


def test_a_model_buy_lane_buys_only_what_the_model_likes(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.mod]\nestimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\nmodel_buy=true\nmodel_top=0.2\n")
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    ticks.history.clear()
    curvemodel._cache.update(mtime=None, data=None)
    now = time.time()
    store.db.execute("INSERT INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    index = variants.crypto_index("KXBTC15M")
    ts, vs = _ticks(now, drift=0.0)
    for t, v in zip(ts, vs, strict=True):
        ticks.history.note(index, t, v)
    shot = LongShot("KXBTC15M-7", "t", "", 0.40, 0.41, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="mod", series="KXBTC15M", side="yes")
    names = curvemodel.NAMES
    base = {"built_at": now, "n": 5000, "names": names, "mu": [0.0] * 19, "sd": [1.0] * 19, "train_days": 9.0, "mean_net": -0.05}
    try:
        none = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert none.action == "pass" and "no model fitted yet" in none.note
        like = {**base, "w": [0.5] + [0.0] * 19, "cuts": {"0.05": 0.4, "0.1": 0.3, "0.2": 0.2, "0.3": 0.1, "0.5": 0.0}}  # predicts +0.5 everywhere: above the 0.2 bar
        (tmp_path / "curve_model.json").write_text(json.dumps(like))
        curvemodel._cache.update(mtime=None, data=None)
        ok = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert ok.action == "bet" and "curve model +0.50 per $1" in ok.note
        dislike = {**base, "w": [0.1] + [0.0] * 19, "cuts": {"0.05": 0.4, "0.1": 0.3, "0.2": 0.2, "0.3": 0.1, "0.5": 0.0}}  # +0.1: positive but under the bar
        (tmp_path / "curve_model.json").write_text(json.dumps(dislike))
        curvemodel._cache.update(mtime=None, data=None)
        low = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert low.action == "pass" and "under the bar" in low.note
        ticks.history.clear()
        unread = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert unread.action == "pass" and "no tick history" in unread.note
    finally:
        ticks.history.clear()
        curvemodel._cache.update(mtime=None, data=None)


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


def test_a_curvature_model_is_fitted_named_and_waited_for_by_a_lane_that_wants_it(tmp_path, monkeypatch):
    random.seed(8)
    rows = [([random.gauss(0, 1) for _ in range(9)], random.uniform(0.3, 0.7), random.gauss(0, 0.5)) for _ in range(3100)]
    model = curvemodel.fit(rows, kind="curvature")
    assert model["kind"] == "curvature" and len(model["mu"]) == 10 and model["names"] == [*curvemodel.QUAD_NAMES, "ask"]
    assert curvemodel.fit([(r[0] + [0.0] * 9, r[1], r[2]) for r in rows])["kind"] == "fibonacci"  # the default kind

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.mod]\nestimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\nmodel_buy=true\nmodel_top=0.2\nmodel_features='curvature'\n")
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    ticks.history.clear()
    curvemodel._cache.update(mtime=None, data=None)
    now = time.time()
    store.db.execute("INSERT INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    index = variants.crypto_index("KXBTC15M")
    ts, vs = _ticks(now, drift=0.0)
    for t, v in zip(ts, vs, strict=True):
        ticks.history.note(index, t, v)
    shot = LongShot("KXBTC15M-7", "t", "", 0.40, 0.41, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="mod", series="KXBTC15M", side="yes")
    cuts = {"0.05": 0.4, "0.1": 0.3, "0.2": 0.2, "0.3": 0.1, "0.5": 0.0}
    try:
        fib = {"built_at": now, "n": 5000, "names": curvemodel.NAMES, "mu": [0.0] * 19, "sd": [1.0] * 19, "train_days": 9.0, "mean_net": -0.05, "w": [0.5] + [0.0] * 19, "cuts": cuts}
        (tmp_path / "curve_model.json").write_text(json.dumps(fib))
        wait = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert wait.action == "pass" and "waiting for the refit" in wait.note  # the fitted model reads Fibonacci readings, the lane wants curvature
        cur = {"kind": "curvature", "built_at": now, "n": 5000, "names": [*curvemodel.QUAD_NAMES, "ask"], "mu": [0.0] * 10, "sd": [1.0] * 10, "train_days": 9.0, "mean_net": -0.05, "w": [0.5] + [0.0] * 10, "cuts": cuts}
        (tmp_path / "curve_model.json").write_text(json.dumps(cur))
        curvemodel._cache.update(mtime=None, data=None)
        ok = runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})
        assert ok.action == "bet" and "curve model +0.50 per $1" in ok.note
    finally:
        ticks.history.clear()
        curvemodel._cache.update(mtime=None, data=None)
