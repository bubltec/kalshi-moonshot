"""The cushion's chance as a second way to buy (cushionmodel, lane field entry_cushion_edge_min): 2026-11-16, user: "it should matter what kind of cushion it has ... fine to buy flat at a high prediction (> 70) and less than 3 mins left"; "needs to be based on the spot cushion and time remaining"; "the more time remaining the more cushion it needs"; "Go live with it". studies/results/2026-11-16-cushion-calibration.txt."""
import dataclasses
import json
import math
import random
import time

import pytest

from kalshi_moonshot import config, cushionmodel, lanes, regime, runner, ticks, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot

from tests.test_entry_phase import Fixed, LANE, path  # noqa: F401

MODEL = {"built_at": 0.0, "days": 5.0, "bands": [{"lo": lo, "hi": hi, "w1": 0.8, "w2": 0.5, "n": 9999} for lo, hi in cushionmodel.BANDS]}


def perp(now, lift_pct=0.0, jump_pct=0.0, noise_pct=0.01, minutes=200):
    """A perp ticking every 30 s: a +-noise_pct zig-zag, `lift_pct` percent added between 6 and 4 minutes ago (after a market that opened 500 s ago, and well before the last 90 s), `jump_pct` over the last 90 s."""
    out, lvl = [], 0.0
    for k in range(minutes * 2, -1, -1):  # k half-minutes ago
        lvl += noise_pct * (1 if k % 2 == 0 else -1)
        if 8 <= k < 12:
            lvl += lift_pct / 4.0
        if k < 3:
            lvl += jump_pct / 3.0
        out.append((now - 30.0 * k, 100.0 * math.exp(lvl / 100.0)))
    return out


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.history.clear()
    variants._broad_memo = (0, None)
    cushionmodel._cache.update(data=None, mtime=0.0)


def read(now, side="yes", left=400.0, **kw):
    p = perp(now, **kw)
    ts, vs = [x[0] for x in p], [x[1] for x in p]
    i = next(k for k, t in enumerate(ts) if t >= now - 500.0)
    return cushionmodel.cushion(ts, vs, now, vs[i], side, left)


def test_unit_the_cushion_is_the_distance_from_the_open_in_typical_moves_for_the_time_left():
    now = time.time()
    assert abs(read(now)) < 1.5  # nothing happened since the open
    up = read(now, lift_pct=0.1)
    assert up > 3  # 0.1% clear, against a typical 90 s move of about 0.01%
    assert read(now, side="no", lift_pct=0.1) == pytest.approx(-up)


def test_unit_the_more_time_left_the_smaller_the_same_distance_counts():
    now = time.time()
    late, early = read(now, lift_pct=0.1, left=120.0), read(now, lift_pct=0.1, left=720.0)
    assert late > early > 0 and late / early == pytest.approx(math.sqrt(720.0 / 120.0), rel=0.01)


def test_unit_no_cushion_without_three_hours_a_reference_or_time_left():
    now = time.time()
    assert read(now, minutes=100) is None
    p = perp(now)
    ts, vs = [x[0] for x in p], [x[1] for x in p]
    assert cushionmodel.cushion(ts, vs, now, 0.0, "yes", 400.0) is None
    assert cushionmodel.cushion(ts, vs, now, 100.0, "yes", 0.0) is None
    assert cushionmodel.cushion(ts, [100.0] * len(ts), now, 100.0, "yes", 400.0) is None  # a flat 3 hours


def test_unit_the_chance_starts_from_kalshis_price_and_the_two_sides_add_to_one():
    yes = cushionmodel.chance(MODEL, 0.80, 2.0, 400.0)
    no = cushionmodel.chance(MODEL, 0.20, -2.0, 400.0)
    assert yes + no == pytest.approx(1.0)
    assert yes > cushionmodel.chance(MODEL, 0.80, 0.0, 400.0) > cushionmodel.chance(MODEL, 0.80, -2.0, 400.0)  # the cushion moves it
    assert cushionmodel.chance(MODEL, 0.80, 2.0, 30.0) is None  # no fit for the last minute
    assert cushionmodel.chance(None, 0.80, 2.0, 400.0) is None


def test_unit_the_edge_asked_grows_with_the_time_left():
    assert cushionmodel.edge_needed(0.03, 180.0) == pytest.approx(0.03)
    assert cushionmodel.edge_needed(0.03, 120.0) < 0.03 < cushionmodel.edge_needed(0.03, 450.0) < cushionmodel.edge_needed(0.03, 780.0)


def test_unit_the_fit_finds_the_weights_the_results_were_made_with():
    rnd = random.Random(7)
    rows = []
    for _ in range(6000):
        mid, c = rnd.uniform(0.1, 0.9), rnd.gauss(0.0, 1.5)
        p = 1.0 / (1.0 + math.exp(-(0.8 * math.log(mid / (1 - mid)) + 0.5 * c)))
        rows.append((mid, c, rnd.random() < p))
    w1, w2 = cushionmodel.fit(rows)
    assert w1 == pytest.approx(0.8, abs=0.1) and w2 == pytest.approx(0.5, abs=0.08)
    model = cushionmodel.model_from({0: rows, 1: rows[:100]}, 123.0)
    assert [b["lo"] for b in model["bands"]] == [cushionmodel.BANDS[0][0]]  # a stretch with too few moments is not fitted


def seed(now, **kw):
    from kalshi_moonshot.watcher import perp_index

    ticks.history.clear()
    for ser in variants.BROAD_SERIES:
        for ts, v in path(now, minutes=300):
            ticks.history.note(variants.crypto_index(ser), ts, v)
    for ts, v in perp(now, **kw):
        ticks.history.note(perp_index("KXBTC15M"), ts, v)


def decide(tmp_path, monkeypatch, now, extra, side="yes", model=MODEL, bid=0.74, ask=0.75):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(extra=extra))
    if model is not None:
        (tmp_path / "cushion_model.json").write_text(json.dumps(model))
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    shot = LongShot("KXBTC15M-7", "t", "", bid, ask, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="ph", series="KXBTC15M", side=side)
    return runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})


BOTH = "entry_spike_min=1.5\nentry_cushion_edge_min=0.03"


def test_integration_a_flat_market_with_a_real_cushion_is_bought_on_the_cushion(tmp_path, monkeypatch):
    now = time.time()
    seed(now, lift_pct=0.1)  # the perp is clear of its open and has been still for four minutes
    out = decide(tmp_path, monkeypatch, now, BOTH)
    assert out.action == "bet" and "[buy on the cushion]" in out.note and "[cushion +" in out.note and "[spike view z " in out.note


def test_integration_a_flat_market_with_no_cushion_is_not_bought_and_both_reasons_are_given(tmp_path, monkeypatch):
    now = time.time()
    seed(now)
    out = decide(tmp_path, monkeypatch, now, BOTH)
    assert out.action == "pass" and "the perp has not moved toward this side" in out.note and "the cushion's chance" in out.note and "the lane asks +" in out.note


def test_integration_the_side_the_cushion_is_against_is_not_bought(tmp_path, monkeypatch):
    now = time.time()
    seed(now, lift_pct=0.1)  # up: a NO bet has the cushion against it
    out = decide(tmp_path, monkeypatch, now, BOTH, side="no", bid=0.24, ask=0.25)
    assert out.action == "pass" and "[cushion -" in out.note


def test_integration_a_move_toward_the_side_is_still_bought_on_the_spike(tmp_path, monkeypatch):
    now = time.time()
    seed(now, jump_pct=0.3)
    out = decide(tmp_path, monkeypatch, now, BOTH)
    assert out.action == "bet" and "[buy on the spike]" in out.note


def test_integration_with_no_fitted_model_the_cushion_cannot_buy(tmp_path, monkeypatch):
    now = time.time()
    seed(now, lift_pct=0.1)
    out = decide(tmp_path, monkeypatch, now, "entry_cushion_edge_min=0.03", model=None)
    assert out.action == "pass" and "[cushion unreadable]" in out.note and "cannot be read" in out.note


def test_integration_off_by_default_nothing_is_read(tmp_path, monkeypatch):
    now = time.time()
    seed(now, lift_pct=0.1)
    out = decide(tmp_path, monkeypatch, now, "")
    assert out.action == "bet" and "[cushion" not in out.note and "[buy on" not in out.note


def test_unit_the_field_changes_a_lanes_version_only_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    base = lanes.LaneSpec(name="x", estimator="market", label="x", price=(0.1, 0.9), hours=(0.0, 1.0), max_spread=0.05, min_volume=0.0)
    assert "entry_cushion_edge_min" not in lanes.settings_of(base, s)
    assert lanes.version_of(base, s) != lanes.version_of(dataclasses.replace(base, entry_cushion_edge_min=0.03), s)
