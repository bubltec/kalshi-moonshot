"""A lane's blackout windows: no new entries inside them, read on the New York clock."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_moonshot import entrywatch, lanes, runner, strategy
from kalshi_moonshot.store import Store
from tests.test_commodities import NOW, ladder
from tests.test_commodities import settings as commodity_settings
from tests.test_two_level import Client, live_event
from tests.test_two_level import settings as two_level_settings


def blackout_spec(tmp_path, *windows):
    s = commodity_settings(tmp_path)
    return s, replace(lanes.spec_for(s, "commodity"), blackout_et=tuple(windows))


def test_the_window_follows_the_new_york_clock_through_daylight_saving(tmp_path):
    _s, spec = blackout_spec(tmp_path, "17:00-18:00")
    at = lambda *a: datetime(*a, tzinfo=UTC)  # noqa: E731
    assert lanes.in_blackout(spec, at(2026, 10, 29, 21, 30))  # 17:30 EDT
    assert not lanes.in_blackout(spec, at(2026, 10, 29, 22, 0))  # 18:00 EDT: the window is over
    assert not lanes.in_blackout(spec, at(2026, 10, 29, 20, 59))  # 16:59 EDT
    assert lanes.in_blackout(spec, at(2026, 11, 2, 22, 30))  # after the clocks go back it is 17:30 EST: the window moved an hour in UTC
    assert not lanes.in_blackout(spec, at(2026, 11, 2, 21, 30))  # 16:30 EST


def test_a_window_may_cross_midnight_and_a_lane_may_have_several(tmp_path):
    _s, spec = blackout_spec(tmp_path, "23:30-00:30", "08:00-08:15")
    at = lambda h, m: datetime(2026, 10, 29, h, m, tzinfo=UTC) + timedelta(hours=4)  # noqa: E731  (UTC time that is h:m in New York)
    assert lanes.in_blackout(spec, at(23, 45)) and lanes.in_blackout(spec, at(0, 10))
    assert not lanes.in_blackout(spec, at(0, 30)) and not lanes.in_blackout(spec, at(23, 29))
    assert lanes.in_blackout(spec, at(8, 5)) and not lanes.in_blackout(spec, at(8, 15))


@pytest.mark.parametrize("bad", ["5pm", "17:00-17:00", "25:00-26:00", "17:00", "17:00-18", "@17:00-18:00", "KXNATGAS15M@", "kxwti@17:00-18:00"])
def test_a_bad_window_fails_when_the_file_is_read(tmp_path, bad):
    toml = f'[lane.x]\nestimator="ladder"\nprice=[0.7,0.9]\nhours=[0.0,1.0]\nblackout_et=["{bad}"]\n'
    with pytest.raises(ValueError, match="lane 'x'"):
        lanes.lane_specs_from(commodity_settings(tmp_path), toml)


def test_a_lane_reads_its_windows_from_strategies_toml(tmp_path):
    toml = '[lane.x]\nestimator="ladder"\nprice=[0.7,0.9]\nhours=[0.0,1.0]\nblackout_et=["17:00-18:00"]\n[lane.y]\ninherit="x"\nblackout_et=[]\n'
    out = lanes.lane_specs_from(commodity_settings(tmp_path), toml)
    assert out["x"].blackout_et == ("17:00-18:00",)
    assert out["y"].blackout_et == ()  # a twin can switch the inherited blackout off


def test_a_market_is_refused_inside_a_blackout_and_accepted_outside_it(tmp_path):
    s, spec = blackout_spec(tmp_path, "11:00-12:00")  # NOW is 11:00 in New York
    m = dict(ladder()[3], yes_ask_dollars="0.8800", yes_bid_dollars="0.8700", volume_fp="500")
    assert strategy.lane_reject_reason(m, replace(spec, blackout_et=()), s, NOW) is None
    assert strategy.lane_reject_reason(m, spec, s, NOW).startswith("blackout")
    assert strategy.lane_reject_reason(m, replace(spec, blackout_et=("12:00-13:00",)), s, NOW) is None  # the same market and moment with the window an hour later


def test_the_entry_watcher_ignores_quotes_inside_a_blackout(tmp_path):
    _s, spec = blackout_spec(tmp_path, "11:00-12:00")
    close = (NOW + timedelta(minutes=5)).isoformat()
    open_spec = replace(spec, blackout_et=(), timed_suffix="15M", entry_elapsed=(0.0, 14.0))
    spec = replace(spec, timed_suffix="15M", entry_elapsed=(0.0, 14.0))
    assert entrywatch.in_window(open_spec, close, NOW)
    assert not entrywatch.in_window(spec, close, NOW)


def test_the_watchlist_is_still_built_during_a_blackout(tmp_path):
    s = two_level_settings(tmp_path)
    now = datetime.now(UTC).astimezone(lanes.ET)
    start, end = now - timedelta(minutes=30), now + timedelta(minutes=30)
    window = f"{start:%H:%M}-{end:%H:%M}"
    spec = lanes.spec_for(s, "commodity")
    assert not lanes.in_blackout(spec, now)
    inside = replace(spec, blackout_et=(window,))
    assert lanes.in_blackout(inside, now)
    watched = runner.scope_lane(s, Client(live_event()), Store(s.db_path), inside)
    assert watched == runner.scope_lane(s, Client(live_event()), Store(tmp_path / "b.db"), spec) >= 1  # the same events, so entries can start the moment the blackout ends


def test_only_a_lane_with_a_blackout_changes_its_version(tmp_path):
    s = commodity_settings(tmp_path)
    plain = lanes.spec_for(s, "commodity")
    assert "blackout_et" not in lanes.settings_of(plain, s)  # every existing lane keeps the version it had
    walled = replace(plain, blackout_et=("17:00-18:00",))
    assert "blackout_et" in lanes.settings_of(walled, s)
    assert lanes.version_of(walled, s) != lanes.version_of(plain, s)


def test_a_window_that_names_a_series_bars_only_that_series(tmp_path):
    _s, spec = blackout_spec(tmp_path, "KXNATGAS15M@17:00-18:00")
    t = datetime(2026, 10, 29, 21, 30, tzinfo=UTC)  # 17:30 in New York
    assert lanes.in_blackout(spec, t, "KXNATGAS15M")
    assert not lanes.in_blackout(spec, t, "KXWTI15M")  # the other energy series keep trading through the hour
    assert not lanes.in_blackout(spec, t)  # with no series given, a series-only window is not counted
    assert not lanes.in_blackout(spec, t + timedelta(minutes=31), "KXNATGAS15M")  # 18:01
    mixed = replace(spec, blackout_et=("KXNATGAS15M@17:00-18:00", "08:00-08:15"))
    assert lanes.in_blackout(mixed, datetime(2026, 10, 29, 12, 5, tzinfo=UTC), "KXWTI15M")  # a plain window bars everything
    assert lanes.in_blackout(mixed, t, "KXNATGAS15M") and not lanes.in_blackout(mixed, t, "KXGOLD15M")


def test_a_named_series_is_refused_by_the_lane_filter_and_the_entry_watcher(tmp_path):
    s, spec = blackout_spec(tmp_path, "KXGOLDD@11:00-12:00")  # the test market's series; NOW is 11:00 in New York
    m = dict(ladder()[3], yes_ask_dollars="0.8800", yes_bid_dollars="0.8700", volume_fp="500")
    assert strategy.lane_reject_reason(m, spec, s, NOW).startswith("blackout")
    other = replace(spec, blackout_et=("KXWTI15M@11:00-12:00",))
    assert strategy.lane_reject_reason(m, other, s, NOW) is None  # a different series' window leaves this market alone
    close = (NOW + timedelta(minutes=5)).isoformat()
    timed = replace(spec, timed_suffix="15M", entry_elapsed=(0.0, 14.0))
    assert not entrywatch.in_window(timed, close, NOW, "KXGOLDD")
    assert entrywatch.in_window(timed, close, NOW, "KXWTI15M")
