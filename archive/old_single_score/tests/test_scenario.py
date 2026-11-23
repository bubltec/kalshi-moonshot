"""The market's scenario as a recorded reading (scenario.py, lane field entry_scenario_view): 2026-11-16, user: "build a structured buy scenario every 15 mins ... based on the previous hour of ... markets"; "where the current target is in comparison with the hour or 15 min scope"; "15, 30, 60"; "Yes, build it"."""
import math
import time

import pytest

from kalshi_moonshot import config, regime, runner, scenario, ticks, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot

from tests.entry_helpers_phase import Fixed, LANE  # noqa: F401


def spot(end, minutes=80, level=100.0, slope_pct=0.0, wiggle_pct=0.02, step=5):
    """An index ticking every `step` s for `minutes` minutes ending at `end`: a sine wiggle of +-wiggle_pct with a 7-minute period, on a line that climbs `slope_pct` percent over the whole stretch."""
    n = minutes * 60 // step
    return [(end - (n - k) * step, level * (1 + slope_pct / 100 * k / n + wiggle_pct / 100 * math.sin(k * step / 420.0 * 2 * math.pi))) for k in range(n + 1)]


def split(rows):
    return [r[0] for r in rows], [r[1] for r in rows]


def test_unit_the_target_is_the_average_of_the_60_seconds_before_the_open():
    opened = 1_000_000.0
    ts = [opened - 120 + k for k in range(121)]
    vs = [100.0] * 60 + [101.0 + (k % 2) for k in range(61)]  # 100 until a minute before the open, then 101 / 102
    assert scenario.target(ts, vs, opened) == pytest.approx(sum(vs[61:]) / 60)
    assert scenario.target(ts[:65], vs[:65], opened) is None  # too few ticks in the last minute


def test_unit_nothing_after_the_moment_is_read():
    opened = 1_000_000.0
    ts, vs = split(spot(opened))
    later_ts, later_vs = split(spot(opened + 600, minutes=90, slope_pct=5.0))  # the same clock, running on with a big move after the open
    base = scenario.at_open(ts, vs, opened)
    cut = [(t, v) for t, v in zip(later_ts, later_vs) if t <= opened]
    assert scenario.at_open(later_ts, later_vs, opened) == scenario.at_open([t for t, _ in cut], [v for _, v in cut], opened)  # ticks after the open change nothing
    assert base is not None and set(base["scopes"]) == {15, 30, 60}
    now = opened + 300
    full_ts, full_vs = split(spot(opened + 600, minutes=90))
    seen = [(t, v) for t, v in zip(full_ts, full_vs) if t <= now]
    a = scenario.cushion(full_ts, full_vs, now, 100.0, "yes", 600.0, 0.0002)
    b = scenario.cushion([t for t, _ in seen], [v for _, v in seen], now, 100.0, "yes", 600.0, 0.0002)
    assert a == pytest.approx(b)  # ticks after now change nothing


def test_unit_the_scope_says_where_the_target_sits_and_how_far_the_index_travelled():
    end = 1_000_000.0
    ts, vs = split(spot(end, slope_pct=1.0, wiggle_pct=0.0))  # a steady climb: the newest price is the high
    up = scenario.scope(ts, vs, end, 60, vs[-1])
    assert up["place"] == pytest.approx(1.0) and up["drift"] > 5
    assert scenario.scope(ts, vs, end, 60, min(vs[-720:]))["place"] == pytest.approx(0.0)
    assert scenario.scope(ts, vs, end, 60, vs[-1] * 1.01)["place"] > 1.0  # beyond the range
    flat_ts, flat_vs = split(spot(end, wiggle_pct=0.0))
    assert scenario.scope(flat_ts, flat_vs, end, 60, 100.0) is None  # a flat scope has no range
    assert scenario.scope(ts[-100:], vs[-100:], end, 60, vs[-1]) is None  # under an hour of ticks


def test_unit_the_cushion_is_signed_by_the_side_and_shrinks_with_time_left():
    now = 1_000_000.0
    ts, vs = [now - 5, now], [100.5, 100.5]
    yes = scenario.cushion(ts, vs, now, 100.0, "yes", 120.0, 0.0005)
    assert yes > 0 and scenario.cushion(ts, vs, now, 100.0, "no", 120.0, 0.0005) == pytest.approx(-yes)
    assert yes / scenario.cushion(ts, vs, now, 100.0, "yes", 480.0, 0.0005) == pytest.approx(2.0)  # four times the time left, half the cushion
    assert scenario.cushion(ts, vs, now + 60, 100.0, "yes", 120.0, 0.0005) is None  # the spot is a minute old
    assert scenario.cushion(ts, vs, now, 100.0, "yes", 0.0, 0.0005) is None


def test_unit_flips_count_changes_of_favorite_not_wobbles():
    assert scenario.flips([0.6, 0.52, 0.48, 0.52, 0.6]) == 0  # never reached 0.45: still YES
    assert scenario.flips([0.6, 0.4, 0.7, 0.3]) == 3
    assert scenario.flips([]) == 0


def test_unit_the_margin_is_the_finish_against_the_target_in_typical_moves():
    close = 1_000_000.0
    ts = [close - 60 + k for k in range(61)]
    assert scenario.margin(ts, [101.0] * 61, close, 100.0, 0.001) == pytest.approx(math.log(1.01) / (0.001 * math.sqrt(15)))
    assert scenario.margin(ts, [99.0] * 61, close, 100.0, 0.001) < 0
    assert scenario.margin(ts, [101.0] * 61, close, 0.0, 0.001) is None


def test_unit_the_label_and_the_note_name_the_reading():
    opening = {"target": 100.0, "scopes": {15: {"place": 0.5, "drift": 0.2, "unit": 0.001}, 60: {"place": 0.9, "drift": 1.4, "unit": 0.001}}}
    assert scenario.label(opening, 0.3) == "target high in the 60 min, cushion thin"
    assert scenario.label(opening, -0.2).endswith("cushion against") and scenario.label(opening, 2.0).endswith("cushion wide")
    tag = scenario.note(opening, 0.3, {15: 0.4, 60: 0.7}, [(0.8, 2), (None, None)])
    assert tag.startswith("[scenario: target high in the 60 min, cushion thin | target in range 15:0.50 60:0.90 | drift 15:+0.2 60:+1.4 | cushion +0.30")
    assert "spot in range now 15:0.40 60:0.70" in tag and "margin/flips +0.8/2 n/a/?]" in tag


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.history.clear()
    ticks.mids.clear()
    variants._broad_memo = (0, None)


def decide(tmp_path, monkeypatch, now, extra, seed=True):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(extra=extra))
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    ticks.history.clear()
    if seed:
        for ts, v in spot(now, minutes=90, slope_pct=0.3):
            ticks.history.note(variants.crypto_index("KXBTC15M"), ts, v)
    opened = now - 500.0
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + 400.0, 100.0, now))
    for k in range(1, 5):  # the four markets before this one, each with a strike
        store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", (f"KXBTC15M-P{k}", "KXBTC15M", opened - 900.0 * (k - 1), 100.0, now))
    store.db.commit()
    for t in range(0, 600, 5):  # the newest earlier market's quotes: YES led, lost the lead, took it back
        mid = 0.7 if t < 200 else 0.3 if t < 400 else 0.65
        ticks.mids.note("KXBTC15M-P1", opened - 900.0 + t, mid - 0.005, mid + 0.005)
    shot = LongShot("KXBTC15M-7", "t", "", 0.88, 0.89, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="ph", series="KXBTC15M", side="yes")
    return runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})


def test_integration_the_note_carries_the_scenario_and_the_bet_still_goes_in(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, time.time(), "entry_scenario_view=true")
    assert out.action == "bet" and "[scenario: target " in out.note
    assert "target in range 15:" in out.note and " 30:" in out.note and " 60:" in out.note and "cushion " in out.note
    assert "last markets margin/flips" in out.note and "/2 " in out.note  # the newest earlier market flipped twice


def test_integration_with_no_index_in_memory_the_note_says_unreadable_and_the_buy_goes_in(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, time.time(), "entry_scenario_view=true", seed=False)
    assert out.action == "bet" and "[scenario unreadable]" in out.note


def test_integration_off_by_default_nothing_is_read(tmp_path, monkeypatch):
    assert "[scenario" not in decide(tmp_path, monkeypatch, time.time(), "").note

