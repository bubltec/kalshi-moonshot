"""The edge bar scaled to the room left (lane field min_edge_room, 2026-11-13, user: a flat edge bar "makes it less likely to buy a 'sure' bet"). Unit tests of the rule, then the rule through the real decision path: lane file -> runner.evaluate -> bet or pass."""


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
    assert lanes.wants_bet(spec(needs_model_edge=False, min_edge_room=0.9), 0.50, 0.90, edge(0.50, 0.90))


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


# ---- record_adjust = false: the lane bets on its estimator's own chance ----

def test_integration_with_the_record_adjustment_off_the_ladders_own_chance_decides(tmp_path, monkeypatch):
    """The 22:26 XRP case: an ask of 0.80, the estimator at 0.907, and a record that pulls it to 0.79."""
    from kalshi_moonshot.calibration import Calibration, Calibrator

    monkeypatch.setattr(Calibrator, "adjust", lambda self, lane, category, ask, p_model, trust=1.0: Calibration(0.79, 0.25, 0.75, 12, -0.04, 61))
    on = decide(tmp_path, monkeypatch, "min_edge_room=0.25", 0.80, 0.907)
    assert on.action == "pass" and "record-adjusted 0.91 -> 0.79" in on.note
    off = decide(tmp_path, monkeypatch, "min_edge_room=0.25\nrecord_adjust=false", 0.80, 0.907)
    assert off.action == "bet" and "record adjustment off for this lane, betting on 0.91" in off.note and "0.79" in off.note
    weak = decide(tmp_path, monkeypatch, "min_edge_room=0.25\nrecord_adjust=false", 0.80, 0.83)
    assert weak.action == "pass"  # the bar still applies to the ladder's own chance


# ---- the stretch on the buy bar: an adjustment, not a rule (2026-11-14, user: "it's an adjustment not a feature. And it needs to consider time left and history ... a tiny factor. Like 1-2c. Maybe less") ----



STRETCH = "min_edge_room=0.10\nrecord_adjust=false\nstretch_max=0.02\nsqueeze_max=0.02"


def far(tmp_path, monkeypatch, bar, ask, p_yes, hours):
    """decide(), for a market with `hours` to its close (the clock is part of the nudge). As in decide(), the shot's ask is one cent over `ask`: at 0.59 with a chance of 0.638 the edge is about 0.008 under the bar."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(bar=bar))
    s = config.load()
    shot = LongShot("KXBTC15M-9", "t", "", ask, ask + 0.01, 0.1, "2099-01-01T00:00:00Z", hours, lane="room", series="KXBTC15M", side="yes")
    return runner.evaluate(s, None, None, Store(s.db_path), shot, estimators={"market": Says(p_yes)})


# ---- no fixed cap (2026-11-15, user: "remove the cap, it needs to hold its own logically. Maybe the high nudge is a good indication") ----
def sub(base, name):
    (base / name).mkdir(exist_ok=True)
    return base / name


UNITS = "min_edge_room=0.10\nrecord_adjust=false\nnudge_units=1.0"


# needs_model_edge (2026-11-16, user: "lets re-work that concept. because 'always' means nothing"): the renamed switch, its old name still read from a lane file, and no lane's version changed by the rename.
def test_unit_the_switch_says_what_it_does_and_defaults_to_needing_an_edge():
    assert lanes.LaneSpec(name="x", estimator="market", label="x", price=(0.1, 0.9), hours=(0.0, 1.0), max_spread=0.05, min_volume=0.0).needs_model_edge is True
    assert not lanes.wants_bet(spec(needs_model_edge=True, min_edge_room=0.9), 0.50, 0.90, edge(0.50, 0.90))  # the model sees no edge: no bet
    assert lanes.wants_bet(spec(needs_model_edge=False, min_edge_room=0.9), 0.50, 0.90, edge(0.50, 0.90))  # the screen is off

