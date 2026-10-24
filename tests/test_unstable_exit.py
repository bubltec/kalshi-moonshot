import pytest

from kalshi_moonshot import config, lanes, watcher


def target(**kw):
    base = {"unstable_gain": 0.03, "unstable_range": 0.06, "unstable_seconds": 20.0}
    t = watcher.Target("KXBTC15M-1", "fav", "yes", 0.50, 0.0, 0.0, 0.0, 30.0, 0.10, 0.0, 0.0, 10.0, 0.0, 3.0, 0.50)
    for k, v in {**base, **kw}.items():
        setattr(t, k, v)
    return t


def quote(bid, ts):
    return {"kind": "quote", "ticker": "KXBTC15M-1", "bid": bid, "ask": bid + 0.01, "ts": ts}


def feed(t, series):
    w = watcher.ProfitWatcher(lambda: config.load(), None, lambda: None)
    w.targets[("KXBTC15M-1", "fav")] = t
    for ts, bid in series:
        w.on_event(quote(bid, ts))
    return w


def test_it_sells_when_up_in_profit_and_the_bid_is_swinging():
    w = feed(target(), [(1000.0, 0.52), (1005.0, 0.60), (1008.0, 0.57)])  # up ~14%, swung 8c inside 20 s
    assert not w._jobs.empty()
    assert w._jobs.get_nowait()[2].startswith("unstable: up")


def test_it_does_nothing_when_the_market_is_calm_even_if_up():
    w = feed(target(), [(1000.0, 0.58), (1005.0, 0.59), (1010.0, 0.58), (1015.0, 0.585)])  # up 16%, but only a 1c range
    assert w._jobs.empty()


def test_it_does_nothing_when_not_in_profit_even_if_the_market_swings():
    w = feed(target(), [(1000.0, 0.45), (1005.0, 0.53), (1008.0, 0.46)])  # swinging 8c, but the bid is under the 50c paid after fees
    assert w._jobs.empty()


def test_a_small_gain_under_the_minimum_does_not_trigger():
    w = feed(target(unstable_gain=0.10), [(1000.0, 0.52), (1005.0, 0.58), (1008.0, 0.55)])  # up 5-10%, under the 10% asked
    assert w._jobs.empty()


def test_it_is_off_by_default_everywhere():
    s = config.load()
    assert s.manual_exit_unstable_gain == 0.0
    assert all(spec.unstable_gain == 0.0 for spec in lanes.lane_specs(s).values() if spec.name != "fx_favorite_unstable")
    w = feed(target(unstable_gain=0.0), [(1000.0, 0.52), (1005.0, 0.60), (1008.0, 0.57)])
    assert w._jobs.empty()


def test_a_lane_that_only_uses_it_is_still_watched_and_its_version_differs():
    s = config.load()
    base = lanes.LaneSpec(name="x", estimator="ladder", label="x", max_spread=0.05, min_volume=0.0, price=(0.1, 0.9), hours=(0, 1))
    from dataclasses import replace

    on = replace(base, unstable_gain=0.03, unstable_range=0.06)
    assert not lanes.takes_profit(base) and lanes.takes_profit(on)
    assert lanes.version_of(base, s) != lanes.version_of(on, s)  # turning it on is a new version of the lane


def test_hand_placed_positions_take_the_settings_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUAL_EXIT_UNSTABLE_GAIN", "0.04")
    monkeypatch.setenv("MANUAL_EXIT_UNSTABLE_RANGE", "0.07")
    s = config.load()
    assert (s.manual_exit_unstable_gain, s.manual_exit_unstable_range, s.manual_exit_unstable_seconds) == (pytest.approx(0.04), pytest.approx(0.07), 20.0)
