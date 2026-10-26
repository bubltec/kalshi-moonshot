"""The curve table: classes of a move so far, the baseline, and the chance the estimator hands a lane."""
import json
import math
import time
from datetime import UTC, datetime

import pytest

from kalshi_moonshot import config, curve, db
from kalshi_moonshot.estimators import CurveEstimator, EstimateFailed
from kalshi_moonshot.strategy import LongShot


def _path(start=100.0, steps=(0.001,) * 8):
    out, v = [], start
    for s in steps:
        v *= math.exp(s)
        out.append(v)
    return out


def test_a_steady_climb_is_ahead_at_its_high_and_rising():
    info = curve.classify(100.0, _path(), 0.001)
    assert info["class"] == 18 + 2 * 6 + 0 * 2 + 1  # ahead, top third, high now, rising
    assert info["minute"] == 8 and info["range_pos"] == pytest.approx(1.0) and info["since_high"] == 0
    assert "ahead" in curve.describe(info["class"]) and "rising" in curve.describe(info["class"])
    assert info["baseline"] > 0.9  # the random walk is nearly sure it finishes above


def test_a_fall_that_bounces_is_behind_and_the_high_was_long_ago():
    info = curve.classify(100.0, _path(100.0, (-0.002, -0.002, -0.002, -0.002, -0.002, 0.001, 0.001, 0.001)), 0.001)
    assert info["class"] // 18 == 0  # behind
    assert info["since_high"] >= 3 and info["class"] % 2 == 1  # the high was at the start, and the last 3 minutes rose


def test_the_table_shifts_the_baseline_in_log_odds_and_leaves_unknown_classes_alone(tmp_path):
    info = curve.classify(100.0, _path(), 0.001)
    table = curve.CurveTable({"coins": {"KXBTC15M": {"8": {str(info["class"]): {"delta": 0.5, "n": 800}}}}})
    p, base, n = table.chance_up("KXBTC15M", info)
    assert n == 800 and base == info["baseline"]
    assert math.log(p / (1 - p)) == pytest.approx(math.log(base / (1 - base)) + 0.5, abs=1e-3)
    other = {**info, "class": 5}
    assert table.chance_up("KXBTC15M", other)[0] == pytest.approx(info["baseline"], abs=1e-9) or table.chance_up("KXBTC15M", other)[2] == 0
    assert table.chance_up("KXETH15M", info)[2] == 0  # another coin's table is never borrowed


def test_outside_the_tables_minutes_there_is_no_class():
    assert curve.classify(100.0, _path(steps=(0.001,) * 3), 0.001) is None
    assert curve.classify(100.0, _path(steps=(0.001,) * 14), 0.001) is None
    assert curve.classify(100.0, _path(), 0.0) is None


def _tape(tmp_path, now, open_ts):
    tape = db.connect(str(tmp_path / "tape.db"), "tape")
    tape.execute("CREATE TABLE IF NOT EXISTS index_ticks (ts REAL NOT NULL, index_id TEXT NOT NULL, value REAL, avg60 REAL, quarter_avg REAL, src_ms REAL)")
    t = open_ts - 250 * 60
    while t <= now:
        minutes_in = max(0.0, (t - open_ts) / 60)
        tape.execute("INSERT INTO index_ticks VALUES (?,?,?,?,?,?)", (t, "BRTI", 100.0 * math.exp(0.0003 * minutes_in + 0.0004 * math.sin(t / 37.0)), None, None, None))
        t += 1.0
    tape.commit()
    return tape


def test_the_estimator_returns_the_tables_chance_for_a_live_move(tmp_path, monkeypatch):
    now = time.time()
    open_ts = now - 8.5 * 60
    close = datetime.fromtimestamp(open_ts + 900, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    tape = _tape(tmp_path, now, open_ts)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    shot = LongShot(ticker="KXBTC15M-26OCT250100-00", title="t", rules="r", bid=0.7, ask=0.71, volume=1.0, close_time=close, hours_to_close=0.1, series="KXBTC15M", side="yes")
    est = CurveEstimator()
    est._local.tape = tape
    with pytest.raises(EstimateFailed):  # no table file yet
        est.estimate(None, s, shot)
    path, before, open_value = curve.minute_closes(tape, "BRTI", open_ts, now)
    info = curve.classify(open_value, path, curve.per_minute_vol(before))
    assert info is not None and info["minute"] == 8
    (tmp_path / "curve_table.json").write_text(json.dumps({"coins": {"KXBTC15M": {"8": {str(info["class"]): {"delta": 0.4, "n": 500}}}}, "months": 3}))
    got = est.estimate(None, s, shot)
    ctx = json.loads(got.context)
    assert ctx["class"] == info["class"] and ctx["windows"] == 500 and ctx["p_up"] == pytest.approx(got.p, abs=1e-4)
    assert got.p > ctx["baseline_up"]  # the +0.4 shift moves the chance up
    assert "curve class" in got.why
