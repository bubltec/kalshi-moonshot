"""The burst exit as a multiple of the market's own normal 3 seconds of dollars, with a floor on the held side's bid (lane burst_ratio, burst_favorite)."""
import dataclasses

import pytest

from kalshi_moonshot import lanes
from kalshi_moonshot.watcher import _market_normal_dollars
from tests.test_settlement import _quiet_minute, _watcher

LANE = "[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nsettlement_flip=true\nburst_ratio={ratio}\nburst_favorite={floor}\nburst_share=0.75\nseries=['KXBTC15M']\nalways=true\n"


def _ratio_watcher(tmp_path, monkeypatch, ratio=6, floor=0.89):
    w, s, store, close = _watcher(tmp_path, monkeypatch)
    (tmp_path / "strategies.toml").write_text(LANE.format(ratio=ratio, floor=floor))
    w.targets.clear()
    w.refresh()
    w.targets[("KXBTC15M-1", "guarded")].close_ts = close  # the helper's decision row closes in 2099; the burst window needs the real close
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.90, "ask": 0.91, "ts": now - 130})
    _quiet_minute(w, now)  # 40 contracts a second at 0.90: about $20 a second, so a normal 3 s is about $60 and 6x is about $360
    return w, store, now


def test_the_lane_settings_reach_the_target(tmp_path, monkeypatch):
    w, _store, _now = _ratio_watcher(tmp_path, monkeypatch)
    t = w.targets[("KXBTC15M-1", "guarded")]
    assert t.burst_ratio == 6 and t.burst_min_dollars == 50.0 and t.burst_favorite == 0.89


def test_the_market_normal_is_the_median_second_of_both_sides_times_three():
    book = {100 - 3 - k: [0.0, 0.0, 12.0, 8.0] for k in range(120)}  # $20 a second for the 120 s before the last 3
    assert _market_normal_dollars(book, 100) == pytest.approx(60.0)
    assert _market_normal_dollars({}, 100) == 0.0


def test_dollars_over_six_times_normal_sell_the_favorite(tmp_path, monkeypatch):
    w, store, now = _ratio_watcher(tmp_path, monkeypatch)
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 1000, "taker": "no", "price": 0.5})  # $500 against a YES holder, over the $360 bar
    assert not w._jobs.empty()
    reason = w._jobs.get_nowait()[2]
    assert reason.startswith("burst:") and "normal" in reason and "6x" in reason


def test_a_print_under_six_times_normal_does_not_sell(tmp_path, monkeypatch):
    w, store, now = _ratio_watcher(tmp_path, monkeypatch)
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 500, "taker": "no", "price": 0.5})  # $250: four times normal
    assert w._jobs.empty()


def test_a_burst_under_the_floor_does_not_sell(tmp_path, monkeypatch):
    w, store, now = _ratio_watcher(tmp_path, monkeypatch, floor=0.89)
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.80, "ask": 0.81, "ts": now + 0.5})  # the favorite is already down to 0.80
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 3000, "taker": "no", "price": 0.5})
    assert w._jobs.empty()


def test_a_burst_with_the_holder_also_being_bought_is_not_a_crash(tmp_path, monkeypatch):
    w, store, now = _ratio_watcher(tmp_path, monkeypatch)
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 1000, "taker": "yes", "price": 0.9})  # the holder's own side bought just as hard
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 1000, "taker": "no", "price": 0.5})
    assert w._jobs.empty()


def test_a_dead_market_needs_the_dollar_minimum(tmp_path, monkeypatch):
    w, store, now = _ratio_watcher(tmp_path, monkeypatch)
    # a market that normally trades about nothing: 6x of almost nothing is still under $50
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 60, "taker": "no", "price": 0.5})  # $30
    assert w._jobs.empty()


def test_the_lane_version_only_changes_for_a_lane_that_sets_the_ratio(tmp_path, monkeypatch):
    from tests.test_bet_scale import _settings

    s = _settings(tmp_path, monkeypatch)
    spec = lanes.spec_for(s, "moonshot")
    assert "burst_ratio" not in lanes.settings_of(spec, s)
    assert lanes.settings_of(dataclasses.replace(spec, burst_ratio=6.0), s)["burst_ratio"] == "6.0"
