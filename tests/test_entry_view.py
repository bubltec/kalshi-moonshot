"""The buy's view (lane field entry_view_scales), shadow: 2026-11-16, user: [0, 15, 60, 180] was meant for the ENTRY, the log ladder for the held bet's long layer."""
import time

import pytest

from kalshi_moonshot import config, curverisk, lanes, runner, ticks, variants
from kalshi_moonshot.strategy import LongShot

from tests.test_entry_phase import Fixed, LANE, path, seed  # noqa: F401  (the same perp paths and lane file)
from kalshi_moonshot import regime
from kalshi_moonshot.store import Store


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.history.clear()
    variants._broad_memo = (0, None)


def decide(tmp_path, monkeypatch, now, extra, side="yes"):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(extra=extra))
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    shot = LongShot("KXBTC15M-7", "t", "", 0.88, 0.89, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="ph", series="KXBTC15M", side=side)
    return runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})


def test_unit_scale_zero_is_the_market_so_far_and_changes_the_reading():
    now = time.time()
    p = path(now, run_pct=0.3)
    ts, vs = [x[0] for x in p], [x[1] for x in p]
    ref = vs[-10]
    with_zero = curverisk.long_chance(ts, vs, now, ref, "yes", 400.0, scales=(0, 15, 60, 180), elapsed=500.0)
    without = curverisk.long_chance(ts, vs, now, ref, "yes", 400.0, scales=(15, 60, 180), elapsed=500.0)
    assert with_zero is not None and without is not None and 0.0 <= with_zero <= 1.0 and with_zero != without


def test_unit_late_in_the_market_the_market_so_far_and_the_15_minute_segment_count_once():
    now = time.time()
    p = path(now, run_pct=0.3, minutes=300)
    ts, vs = [x[0] for x in p], [x[1] for x in p]
    same = curverisk.multiscale_rate(ts, vs, now, (0, 15, 60, 180), elapsed=15 * 60.0)
    assert same == pytest.approx(curverisk.multiscale_rate(ts, vs, now, (15, 60, 180)))
    near = curverisk.multiscale_rate(ts, vs, now, (0, 15, 60, 180), elapsed=14 * 60.0)  # one minute apart: still one segment
    assert near == pytest.approx(curverisk.multiscale_rate(ts, vs, now, (14, 60, 180)))
    early = curverisk.multiscale_rate(ts, vs, now, (0, 15, 60, 180), elapsed=4 * 60.0)
    assert early != pytest.approx(curverisk.multiscale_rate(ts, vs, now, (15, 60, 180)))  # early, the market so far is its own segment


def test_unit_the_live_ladder_is_unchanged_by_the_merge():
    now = time.time()
    p = path(now, run_pct=0.3, minutes=400)
    ts, vs = [x[0] for x in p], [x[1] for x in p]
    assert curverisk.multiscale_rate(ts, vs, now, (8, 16, 32, 64, 128, 256)) is not None


def test_integration_the_buy_note_carries_the_entry_view_and_the_bet_still_goes_in(tmp_path, monkeypatch):
    now = time.time()
    seed(now, minutes=300)
    out = decide(tmp_path, monkeypatch, now, "entry_view_scales=[0,15,60,180]")
    assert out.action == "bet" and "[entry view " in out.note and "at 0,15,60,180 min]" in out.note


def test_integration_the_view_is_shadow_it_never_blocks_a_buy(tmp_path, monkeypatch):
    now = time.time()
    seed(now, run_pct=0.5, minutes=300)  # the perp ran up: a NO bet has the view against it
    out = decide(tmp_path, monkeypatch, now, "entry_view_scales=[0,15,60,180]", side="no")
    assert out.action == "bet" and "[entry view " in out.note


def test_integration_the_view_reads_the_side(tmp_path, monkeypatch):
    now = time.time()
    seed(now, run_pct=0.5, minutes=300)
    yes = decide(tmp_path, monkeypatch, now, "entry_view_scales=[0,15,60,180]", side="yes").note
    no = decide(tmp_path, monkeypatch, now, "entry_view_scales=[0,15,60,180]", side="no").note
    pick = lambda n: float(n.split("[entry view ")[1].split(" ")[0])
    assert pick(yes) > pick(no)


def test_integration_with_no_perp_the_note_says_unreadable_and_the_buy_goes_in(tmp_path, monkeypatch):
    now = time.time()
    ticks.history.clear()
    out = decide(tmp_path, monkeypatch, now, "entry_view_scales=[0,15,60,180]")
    assert out.action == "bet" and "[entry view unreadable]" in out.note


def test_integration_off_by_default_nothing_is_read(tmp_path, monkeypatch):
    now = time.time()
    seed(now, minutes=300)
    assert "[entry view" not in decide(tmp_path, monkeypatch, now, "").note


def test_unit_the_field_changes_a_lanes_version_only_when_set(tmp_path, monkeypatch):
    import dataclasses

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    base = lanes.LaneSpec(name="x", estimator="market", label="x", price=(0.1, 0.9), hours=(0.0, 1.0), max_spread=0.05, min_volume=0.0)
    assert "entry_view_scales" not in lanes.settings_of(base, s)
    on = dataclasses.replace(base, entry_view_scales=(0, 15, 60, 180))
    assert lanes.settings_of(on, s)["entry_view_scales"] == "(0, 15, 60, 180)" and lanes.version_of(base, s) != lanes.version_of(on, s)


def test_integration_a_history_shorter_than_the_longest_scale_is_unreadable_not_a_guess(tmp_path, monkeypatch):
    now = time.time()
    seed(now)  # 90 minutes of ticks, the 180-minute scale needs three hours
    assert "[entry view unreadable]" in decide(tmp_path, monkeypatch, now, "entry_view_scales=[0,15,60,180]").note
