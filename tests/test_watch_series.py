"""WATCH_SERIES opts in to the series the bot follows: lane discovery and the tape skip everything else, and a live lane keeps its own series."""
from kalshi_moonshot import config, runner
from kalshi_moonshot.lanes import tiered_lanes


def test_no_list_follows_everything(monkeypatch):
    monkeypatch.setenv("WATCH_SERIES", "")
    assert runner.watched(config.load()) is None


def test_list_limits_other_lanes_but_never_a_live_lane(monkeypatch):
    monkeypatch.setenv("WATCH_SERIES", "KXNOTHING")
    base = config.load()
    lanes = [spec for spec in tiered_lanes(base) if len(runner._all_series(spec, base)) > 1]
    assert lanes, "the default lanes should include one that watches several series"
    live = lanes[0]
    monkeypatch.setenv("LIVE_LANES", live.name)
    s = config.load()
    keep = runner.watched(s)
    assert keep >= set(runner._all_series(live, s)) and "KXNOTHING" in keep
    assert set(runner.lane_series(live, s)) == set(runner._all_series(live, s))
    for spec in tiered_lanes(s):
        if spec.name != live.name:
            assert set(runner.lane_series(spec, s)) <= keep
