"""The edge bar scaled to the room left (lane field min_edge_room, 2026-11-13, user: a flat edge bar "makes it less likely to buy a 'sure' bet"). Unit tests of the rule, then the rule through the real decision path: lane file -> runner.evaluate -> bet or pass."""

import dataclasses

import pytest

from kalshi_moonshot import config, lanes, runner
from kalshi_moonshot.estimators import Estimate
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot, edge, fee_per_contract


def spec(**kw):
    return lanes.LaneSpec("x", "ladder", "x", (0.70, 0.97), (0, 1), 0.05, 0.0, **kw)


def test_unit_the_scaled_edge_is_the_edge_over_the_room_between_cost_and_certainty():
    for ask, p in ((0.90, 0.95), (0.70, 0.80), (0.50, 0.55)):
        e = edge(p, ask)
        room = 1.0 - ask - fee_per_contract(ask)
        assert lanes.scaled_edge(p, e) == pytest.approx(e / room)
    assert lanes.scaled_edge(0.95, edge(0.95, 0.90)) > lanes.scaled_edge(0.55, edge(0.55, 0.50))  # the same five cents is a much larger share of the room at 0.90
    assert lanes.scaled_edge(1.0, 0.0) == -1.0  # no room left: no edge


def test_unit_the_scaled_bar_lets_a_high_priced_bet_through_that_a_flat_bar_never_can():
    scaled, flat = spec(min_edge_room=0.3), spec(min_edge=0.10)
    assert lanes.wants_bet(scaled, 0.95, 0.90, edge(0.95, 0.90))  # the chance of losing is half what the price says
    assert not lanes.wants_bet(flat, 0.95, 0.90, edge(0.95, 0.90))  # 10 cents over a price of 0.90 would need a certainty
    assert not lanes.wants_bet(scaled, 0.92, 0.90, edge(0.92, 0.90))  # a sixth of the room: not enough
    assert lanes.wants_bet(scaled, 0.81, 0.70, edge(0.81, 0.70)) and not lanes.wants_bet(scaled, 0.76, 0.70, edge(0.76, 0.70))  # at 0.70 the bar sits just over a chance of 0.80
    assert not lanes.wants_bet(scaled, 0.85, 0.90, edge(0.85, 0.90))  # under the price: never


def test_unit_with_both_bars_both_must_pass_and_always_still_skips_them():
    both = spec(min_edge=0.03, min_edge_room=0.3)
    assert lanes.wants_bet(both, 0.81, 0.70, edge(0.81, 0.70))
    assert not lanes.wants_bet(both, 0.935, 0.90, edge(0.935, 0.90))  # clears the scaled bar (about 0.3 of the room) but is under 3 cents
    assert lanes.wants_bet(spec(always=True, min_edge_room=0.9), 0.50, 0.90, edge(0.50, 0.90))


def test_unit_a_lane_without_the_bar_keeps_its_version_and_a_lane_with_it_gets_a_new_one(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    plain = spec(min_edge=0.015)
    assert "min_edge_room" not in lanes.settings_of(plain, s)
    with_bar = dataclasses.replace(plain, min_edge_room=0.3)
    assert lanes.settings_of(with_bar, s)["min_edge_room"] == "0.3"
    assert lanes.version_of(plain, s) != lanes.version_of(with_bar, s)


class Says:
    def __init__(self, p_yes):
        self.p_yes = p_yes

    def estimate(self, client, settings, shot):
        return Estimate(self.p_yes, "fixed")


LANE = "[lane.room]\nestimator='market'\nprice=[0.50,0.95]\nhours=[0.0,1.0]\nfixed_stake=1.0\nseries=['KXBTC15M']\n{bar}\n"


def decide(tmp_path, monkeypatch, bar, ask, p_yes):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(bar=bar))
    s = config.load()
    store = Store(s.db_path)
    shot = LongShot("KXBTC15M-9", "t", "", ask, ask + 0.01, 0.1, "2099-01-01T00:00:00Z", 0.1, lane="room", series="KXBTC15M", side="yes")
    return runner.evaluate(s, None, None, store, shot, estimators={"market": Says(p_yes)})


def test_integration_the_lane_file_setting_decides_the_bet_through_evaluate(tmp_path, monkeypatch):
    assert lanes.lane_specs_from(config.load(), LANE.format(bar="min_edge_room=0.3"))["room"].min_edge_room == 0.3
    assert decide(tmp_path, monkeypatch, "min_edge_room=0.3", 0.90, 0.95).action == "bet"  # a sure-looking bet the scaled bar takes
    assert decide(tmp_path, monkeypatch, "min_edge_room=0.3", 0.90, 0.92).action == "pass"
    assert decide(tmp_path, monkeypatch, "min_edge_room=0.3", 0.70, 0.76).action == "pass"
    assert decide(tmp_path, monkeypatch, "min_edge_room=0.3", 0.70, 0.81).action == "bet"


def test_integration_the_flat_bar_refuses_the_same_sure_looking_bet(tmp_path, monkeypatch):
    assert decide(tmp_path, monkeypatch, "min_edge=0.10", 0.90, 0.95).action == "pass"
    assert decide(tmp_path, monkeypatch, "min_edge=0.10", 0.70, 0.85).action == "bet"
