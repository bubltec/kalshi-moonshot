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
