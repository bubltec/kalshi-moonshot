"""The forecast estimator: slice moves from index ticks, a prediction from the weights file, and the chance it hands a lane."""
import json
import math
import time
from datetime import datetime

import pytest

from kalshi_moonshot import config, db, forecast
from kalshi_moonshot.estimators import EstimateFailed, EstimateShare, ForecastEstimator
from kalshi_moonshot.strategy import LongShot

SIGMA = {"BRTI": {str(h): 0.01 for h in (3, 7, 15, 30, 60, 120)}}


def _weights(tmp_path, beta):
    path = tmp_path / "w.json"
    path.write_text(json.dumps({"beta": beta, "sigma": SIGMA, "horizon": 5, "cutoff": 1.0}))
    return str(path)


def _tape(tmp_path, now, step=0.0):
    """An index that rose `step` (log) per minute over the last two hours and a bit more."""
    tape = db.connect(str(tmp_path / "tape.db"), "tape")
    tape.execute("CREATE TABLE IF NOT EXISTS index_ticks (ts REAL NOT NULL, index_id TEXT NOT NULL, value REAL, avg60 REAL, quarter_avg REAL, src_ms REAL)")
    for minute in range(130):
        tape.execute("INSERT INTO index_ticks VALUES (?,?,?,?,?,?)", (now - minute * 60.0 - 0.5, "BRTI", 100.0 * math.exp(-step * minute), None, None, None))
    return tape


def _shot(side="yes", ask=0.70):
    return LongShot(ticker="KXBTC15M-26OCT251100-00", title="t", rules="r", bid=ask - 0.01, ask=ask, volume=1.0, close_time=datetime.now().isoformat(), hours_to_close=0.1,
                    series="KXBTC15M", side=side)


def test_flat_index_forecasts_the_intercept(tmp_path):
    now = time.time()
    tape = _tape(tmp_path, now)
    w = forecast.load_weights(_weights(tmp_path, [0.004, 1, 1, 1, 1, 1, 1]))
    moves = forecast.slice_moves(tape, w, "BRTI", "yes", now)
    assert moves == pytest.approx([0.0] * 6, abs=1e-9)
    assert forecast.predict(w, moves) == pytest.approx(0.004)


def test_a_rising_index_is_signed_toward_the_side(tmp_path):
    now = time.time()
    tape = _tape(tmp_path, now, step=0.0005)
    w = forecast.load_weights(_weights(tmp_path, [0, 0.01, 0, 0, 0, 0, 0]))
    up = forecast.slice_moves(tape, w, "BRTI", "yes", now)
    down = forecast.slice_moves(tape, w, "BRTI", "no", now)
    assert up[0] > 0 and down[0] == pytest.approx(-up[0])
    assert up[0] == pytest.approx(0.0005 * 3 / (0.01 * math.sqrt(3 / 3)))  # a 3-minute log move over a 1% usual size


def test_missing_reading_gives_none(tmp_path):
    now = time.time()
    tape = _tape(tmp_path, now)
    w = forecast.load_weights(_weights(tmp_path, [0, 0, 0, 0, 0, 0, 0]))
    assert forecast.slice_moves(tape, w, "BRTI", "yes", now + 4 * 3600) is None
    assert forecast.slice_moves(tape, w, "ETHUSD_RTI", "yes", now) is None


def test_weights_with_the_wrong_length_are_refused(tmp_path):
    with pytest.raises(ValueError):
        forecast.load_weights(_weights(tmp_path, [0, 0, 0]))


def test_estimator_returns_ask_plus_move_and_flips_for_no(tmp_path, monkeypatch):
    now = time.time()
    tape = _tape(tmp_path, now)
    monkeypatch.setenv("FORECAST_WEIGHTS", _weights(tmp_path, [0.01, 0, 0, 0, 0, 0, 0]))
    s = config.load()
    est = ForecastEstimator()
    est._local.tape = tape
    yes = est.estimate(None, s, _shot("yes", 0.70))
    no = est.estimate(None, s, _shot("no", 0.70))
    assert yes.p == pytest.approx(0.71) and no.p == pytest.approx(0.29)  # the NO side's chance is 0.71 too, so its YES chance is 0.29
    assert json.loads(yes.context)["move"] == pytest.approx(0.01)


def test_estimator_fails_cleanly_without_the_file_or_a_reading(tmp_path, monkeypatch):
    monkeypatch.setenv("FORECAST_WEIGHTS", str(tmp_path / "missing.json"))
    est = ForecastEstimator()
    with pytest.raises(EstimateFailed):
        est.estimate(None, config.load(), _shot())


def test_the_forecast_is_never_shared_between_lanes():
    assert "forecast" in EstimateShare.UNSHARED


def test_recalled_move_is_the_average_of_the_closest_past_moments(tmp_path):
    path = tmp_path / "w.json"
    cases = [[1.0, 0, 0, 0, 0, 0, 0.02], [1.1, 0, 0, 0, 0, 0, 0.04], [-5.0, 0, 0, 0, 0, 0, -0.10]]
    path.write_text(json.dumps({"beta": [0.0] * 7, "sigma": SIGMA, "horizon": 5, "k": 2, "blend": 0.5, "cases": cases}))
    w = forecast.load_weights(str(path))
    assert forecast.recalled(w, [1.05, 0, 0, 0, 0, 0]) == pytest.approx(0.03)  # the two near cases, not the far one
    blended, reg, memory = forecast.forecast_move(w, [1.05, 0, 0, 0, 0, 0])
    assert reg == 0.0 and memory == pytest.approx(0.03) and blended == pytest.approx(0.015)


def test_no_cases_means_the_regression_alone(tmp_path):
    w = forecast.load_weights(_weights(tmp_path, [0.004, 0, 0, 0, 0, 0, 0]))
    assert forecast.forecast_move(w, [0.0] * 6) == (0.004, 0.004, None)


def test_least_squares_recovers_a_line():
    from kalshi_moonshot.forecast_fit import least_squares

    xs = [[float(i), float(i % 3)] for i in range(40)]
    ys = [0.5 + 2.0 * x[0] - 1.0 * x[1] for x in xs]
    b = least_squares(xs, ys)
    assert b == pytest.approx([0.5, 2.0, -1.0], abs=1e-6)


def test_the_fit_reads_the_same_slices_as_the_estimator(tmp_path):
    from kalshi_moonshot.forecast_fit import _slice_moves_from

    now = time.time()
    tape = _tape(tmp_path, now, step=0.0007)
    w = forecast.load_weights(_weights(tmp_path, [0, 1, 1, 1, 1, 1, 1]))
    rows = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id='BRTI' ORDER BY ts").fetchall()
    arrays = ([r[0] for r in rows], [r[1] for r in rows])
    for side in ("yes", "no"):
        assert _slice_moves_from(arrays, w, "BRTI", side, now) == pytest.approx(forecast.slice_moves(tape, w, "BRTI", side, now))


def test_a_forecast_lane_turns_the_refit_on(monkeypatch):
    from kalshi_moonshot import forecast_fit

    monkeypatch.setenv("FORECAST_REFIT_HOURS", "0")
    assert not forecast_fit.wanted(config.load())


def test_the_forecast_stays_on_the_0001_to_9999_scale(tmp_path, monkeypatch):
    now = time.time()
    tape = _tape(tmp_path, now)
    est = ForecastEstimator()
    est._local.tape = tape
    monkeypatch.setenv("FORECAST_WEIGHTS", _weights(tmp_path, [0.05, 0, 0, 0, 0, 0, 0]))
    ctx = json.loads(est.estimate(None, config.load(), _shot("yes", 0.91)).context)
    assert ctx["move"] == pytest.approx(0.05)  # 8.99c of room: unchanged
    got = est.estimate(None, config.load(), _shot("yes", 0.97))
    ctx = json.loads(got.context)
    assert ctx["move"] == pytest.approx(0.0299) and ctx["uncapped"] == pytest.approx(0.05)  # only 2.99c of room under 99.99c
    assert got.p == pytest.approx(0.9999)
    monkeypatch.setenv("FORECAST_WEIGHTS", _weights(tmp_path, [-0.30, 0, 0, 0, 0, 0, 0]))
    low = est.estimate(None, config.load(), _shot("yes", 0.20))
    assert json.loads(low.context)["move"] == pytest.approx(-0.1999)  # a price cannot fall below 0.01c
    assert low.p == pytest.approx(0.0001)


def test_order_variants_fill_only_when_the_ask_comes_down():
    from kalshi_moonshot.forecast_fit import order_variants

    got = order_variants(0.80, 0.79, 0.04, [0.80, 0.795, 0.79, 0.85], True)
    assert got["take the ask"] > 0 and got["rest at the bid"] == pytest.approx((1 - 0.79) / 0.79)  # the ask reached 0.79
    assert got["rest 1c under the ask"] == pytest.approx((1 - 0.79) / 0.79)
    assert got["rest 1 x last-30s move under the ask"] is None  # 4c under the ask is 0.76: never reached
    lost = order_variants(0.80, 0.79, 0.0, [0.79], False)
    assert lost["rest at the bid"] == -1.0 and lost["take the ask"] == -1.0


def test_order_variants_stay_on_the_price_scale():
    from kalshi_moonshot.forecast_fit import order_variants

    got = order_variants(0.05, 0.04, 0.5, [0.0001], True)
    assert got["rest 2 x last-30s move under the ask"] is None  # a negative price is impossible
    assert got["rest at the bid"] is not None


def test_a_skip_rule_trips_only_on_its_own_end_of_the_range():
    gates = {"slice_7_15": {"worse": "high", "cutoff": 0.5}, "held_min": {"worse": "high", "cutoff": 4.0}, "prior_15": {"worse": "low", "cutoff": -5.0}}
    assert forecast.gates_tripped(gates, {"slice_7_15": 0.2, "held_min": 1.0, "prior_15": 3.0}) == []
    got = forecast.gates_tripped(gates, {"slice_7_15": 0.9, "held_min": None, "prior_15": -8.0})
    assert len(got) == 2 and "slice_7_15" in got[0] and "prior_15" in got[1]  # a missing value never trips a rule


def test_a_tripped_rule_makes_the_lane_pass_and_is_saved(tmp_path, monkeypatch):
    now = time.time()
    tape = _tape(tmp_path, now, step=0.0007)  # a steady climb, so every slice is strongly positive
    monkeypatch.setenv("FORECAST_WEIGHTS", _weights(tmp_path, [0.02, 0, 0, 0, 0, 0, 0]))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "forecast_gates.json").write_text(json.dumps({"gates": {"slice_7_15": {"worse": "high", "cutoff": 0.0}}}))
    est = ForecastEstimator()
    est._local.tape = tape
    shot = _shot("yes", 0.70)
    got = est.estimate(None, config.load(), shot)
    ctx = json.loads(got.context)
    assert ctx["skipped"] and ctx["move"] == pytest.approx(0.02)  # the forecast is still saved
    assert got.p < shot.ask  # the chance is under the ask: the lane passes
    assert got.why.startswith("skipped by the record")
    (tmp_path / "forecast_gates.json").write_text(json.dumps({"gates": {}}))
    assert json.loads(est.estimate(None, config.load(), shot).context)["skipped"] == [] and est.estimate(None, config.load(), shot).p > shot.ask
