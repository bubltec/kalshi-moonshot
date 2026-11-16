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
    assert lanes.wants_bet(spec(needs_model_edge=False, min_edge_room=0.9), 0.50, 0.90, edge(0.50, 0.90))


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


def test_unit_a_lane_keeps_its_version_unless_the_adjustment_is_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    plain = spec(min_edge=0.015)
    assert "record_adjust" not in lanes.settings_of(plain, s)
    off = dataclasses.replace(plain, record_adjust=False)
    assert lanes.settings_of(off, s)["record_adjust"] == "False" and lanes.version_of(plain, s) != lanes.version_of(off, s)


# ---- the stretch on the buy bar: an adjustment, not a rule (2026-11-14, user: "it's an adjustment not a feature. And it needs to consider time left and history ... a tiny factor. Like 1-2c. Maybe less") ----

def test_unit_edge_miss_is_the_shortfall_under_the_scaled_bar_only():
    sp = spec(min_edge_room=0.10)
    e = edge(0.629, 0.58)
    room = 1.0 - 0.58 - fee_per_contract(0.58)
    assert lanes.edge_miss(sp, 0.629, 0.58, e) == pytest.approx(0.10 * room - e)  # the BTC YES of 13:03: about 0.008
    assert lanes.edge_miss(sp, 0.70, 0.58, edge(0.70, 0.58)) < 0  # over the bar: no miss
    assert lanes.edge_miss(spec(min_edge=0.03), 0.629, 0.58, e) is None  # no scaled bar
    assert lanes.edge_miss(spec(min_edge=0.05, min_edge_room=0.10), 0.629, 0.58, e) is None  # the flat bar fails too: not reached for
    assert lanes.edge_miss(spec(needs_model_edge=False, min_edge_room=0.10), 0.629, 0.58, e) is None


def test_unit_the_nudge_is_the_cap_scaled_down_by_history_and_by_the_clock():
    assert lanes.nudge(0.02, 1.0, 1.0) == pytest.approx(0.02)  # never more than the cap
    assert lanes.nudge(0.02, 0.5, 1.0) == pytest.approx(0.01)
    assert lanes.nudge(0.02, 1.0, 0.25) == pytest.approx(0.005)
    assert lanes.nudge(0.02, 0.0, 1.0) == 0.0 and lanes.nudge(0.02, 1.0, 0.0) == 0.0  # either one saying no means no nudge
    assert lanes.nudge(0.02, 5.0, 5.0) == pytest.approx(0.02) and lanes.nudge(0.0, 1.0, 1.0) == 0.0


def test_unit_the_buys_history_share_is_how_far_the_side_has_moved_its_way():
    assert lanes.trend_share(None) == 0.0 and lanes.trend_share(-0.5) == 0.0 and lanes.trend_share(0.0) == 0.0
    assert lanes.trend_share(0.5) == 0.5 and lanes.trend_share(1.0) == 1.0 and lanes.trend_share(2.5) == 1.0
    assert lanes.trend_share(3.5) == 0.0  # over three typical moves: an anomaly, no rule applies


STRETCH = "min_edge_room=0.10\nrecord_adjust=false\nstretch_max=0.02\nsqueeze_max=0.02"


def far(tmp_path, monkeypatch, bar, ask, p_yes, hours):
    """decide(), for a market with `hours` to its close (the clock is part of the nudge). As in decide(), the shot's ask is one cent over `ask`: at 0.59 with a chance of 0.638 the edge is about 0.008 under the bar."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(bar=bar))
    s = config.load()
    shot = LongShot("KXBTC15M-9", "t", "", ask, ask + 0.01, 0.1, "2099-01-01T00:00:00Z", hours, lane="room", series="KXBTC15M", side="yes")
    return runner.evaluate(s, None, None, Store(s.db_path), shot, estimators={"market": Says(p_yes)})


def test_integration_a_buy_under_a_cent_short_with_the_side_trending_its_way_is_bet_and_tagged(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "side_trend_z", lambda s, shot, seconds, now=None: 1.5)
    got = far(tmp_path, monkeypatch, STRETCH, 0.58, 0.638, 0.2)  # 0.008 under the bar, 12 minutes left: a nudge of 0.02 x 1 x 0.8 = 0.016
    assert got.action == "bet" and "[stretch buy" in got.note


def test_integration_the_same_shortfall_late_in_the_market_is_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "side_trend_z", lambda s, shot, seconds, now=None: 1.5)
    got = far(tmp_path, monkeypatch, STRETCH, 0.58, 0.638, 0.05)  # 3 minutes left: a nudge of 0.02 x 1 x 0.2 = 0.004, under the 0.008 shortfall
    assert got.action == "pass" and "[stretch buy" not in got.note


def test_integration_the_same_shortfall_with_a_weak_trend_is_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "side_trend_z", lambda s, shot, seconds, now=None: 0.3)  # a nudge of 0.02 x 0.3 x 0.8 = 0.005
    assert far(tmp_path, monkeypatch, STRETCH, 0.58, 0.638, 0.2).action == "pass"


def test_integration_trending_against_or_an_anomaly_gives_no_nudge(tmp_path, monkeypatch):
    for zed in (-1.5, 3.5, None):
        monkeypatch.setattr(runner, "side_trend_z", lambda s, shot, seconds, now=None, zed=zed: zed)
        assert far(tmp_path, monkeypatch, STRETCH, 0.58, 0.638, 0.2).action == "pass"


def test_integration_a_miss_of_more_than_the_cap_is_never_stretched(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "side_trend_z", lambda s, shot, seconds, now=None: 1.5)
    assert far(tmp_path, monkeypatch, STRETCH, 0.58, 0.60, 0.25).action == "pass"  # about 0.04 under the bar: twice the two-cent cap


def test_integration_with_the_stretch_off_or_no_squeeze_beside_it_the_near_miss_is_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "side_trend_z", lambda s, shot, seconds, now=None: 1.5)
    assert far(tmp_path, monkeypatch, "min_edge_room=0.10\nrecord_adjust=false", 0.58, 0.638, 0.2).action == "pass"
    assert far(tmp_path, monkeypatch, "min_edge_room=0.10\nrecord_adjust=false\nstretch_max=0.02", 0.58, 0.638, 0.2).action == "pass"  # the pair works together


def test_integration_the_ladder_screen_keeps_a_stretched_near_miss_and_drops_it_otherwise(tmp_path, monkeypatch):
    from kalshi_moonshot.estimators import LadderEstimator

    class Pricer:
        def price_markets(self, markets, s, now):
            return {m["ticker"]: (0.629, "note") for m in markets}

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    sp = dataclasses.replace(spec(min_edge_room=0.10, stretch_max=0.02, squeeze_max=0.02), price=(0.30, 0.90))
    sh = LongShot("KXBTCD-X-T1", "t", "r", 0.57, 0.58, 100.0, "2099-01-01T00:00:00Z", 0.2, lane="x", series="KXBTCD", event_ticker="KXBTCD-X", side="yes")
    rows = [{"ticker": "KXBTCD-X-T1", "event_ticker": "KXBTCD-X"}]
    monkeypatch.setattr(runner, "side_trend_z", lambda s, shot, seconds, now=None: 1.5)
    assert [k.ticker for k in LadderEstimator(Pricer()).screen([sh], rows, sp, s, None)] == ["KXBTCD-X-T1"]
    monkeypatch.setattr(runner, "side_trend_z", lambda s, shot, seconds, now=None: -1.5)
    est = LadderEstimator(Pricer())
    assert est.screen([sh], rows, sp, s, None) == [] and len(est.dropped) == 1


def test_unit_the_stretch_changes_a_lanes_version_only_when_it_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    plain = spec(min_edge_room=0.10)
    assert "stretch_max" not in lanes.settings_of(plain, s)
    on = dataclasses.replace(plain, stretch_max=0.02)
    assert lanes.settings_of(on, s)["stretch_max"] == "0.02" and lanes.version_of(plain, s) != lanes.version_of(on, s)


# ---- no fixed cap (2026-11-15, user: "remove the cap, it needs to hold its own logically. Maybe the high nudge is a good indication") ----
def sub(base, name):
    (base / name).mkdir(exist_ok=True)
    return base / name


UNITS = "min_edge_room=0.10\nrecord_adjust=false\nnudge_units=1.0"


def test_integration_without_a_cap_a_miss_over_two_cents_is_bet_when_the_market_moves_enough_and_everything_agrees(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "side_trend_z", lambda s, shot, seconds, now=None: 1.5)
    monkeypatch.setattr(runner, "side_trend_read", lambda s, shot, seconds, now=None: (0.12, 0.08))  # a 60 s typical move of 8 cents
    assert far(tmp_path, monkeypatch, STRETCH, 0.58, 0.60, 0.25).action == "pass"  # the capped stretch: about 0.04 under the bar, twice the cap (test above)
    got = far(sub(tmp_path, "u"), monkeypatch, UNITS, 0.58, 0.60, 0.25)  # 0.08 x 1 (the history share) x 1 (15 minutes left) = 8 cents allowed
    assert got.action == "bet" and "[stretch buy" in got.note and "no cap:" in got.note


def test_integration_without_a_cap_a_quiet_market_allows_a_smaller_miss_and_a_weak_trend_or_a_late_clock_allows_none(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "side_trend_z", lambda s, shot, seconds, now=None: 1.5)
    monkeypatch.setattr(runner, "side_trend_read", lambda s, shot, seconds, now=None: (0.01, 0.01))  # a quiet market: a typical move of one cent
    assert far(sub(tmp_path, "q"), monkeypatch, UNITS, 0.58, 0.60, 0.25).action == "pass"  # 4 cents under the bar, 1 cent allowed
    monkeypatch.setattr(runner, "side_trend_read", lambda s, shot, seconds, now=None: (0.12, 0.08))
    monkeypatch.setattr(runner, "side_trend_z", lambda s, shot, seconds, now=None: -1.5)
    assert far(sub(tmp_path, "w"), monkeypatch, UNITS, 0.58, 0.60, 0.25).action == "pass"  # trending against: no history share
    monkeypatch.setattr(runner, "side_trend_z", lambda s, shot, seconds, now=None: 1.5)
    assert far(sub(tmp_path, "l"), monkeypatch, UNITS, 0.58, 0.60, 0.01).action == "pass"  # a minute left: the clock share is a few percent
    monkeypatch.setattr(runner, "side_trend_read", lambda s, shot, seconds, now=None: None)
    assert far(sub(tmp_path, "n"), monkeypatch, UNITS, 0.58, 0.60, 0.25).action == "pass"  # the unit cannot be read: no nudge


def test_unit_nudge_units_changes_a_lanes_version_only_when_it_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    plain = spec(min_edge_room=0.10)
    assert "nudge_units" not in lanes.settings_of(plain, s)
    on = dataclasses.replace(plain, nudge_units=1.0)
    assert lanes.settings_of(on, s)["nudge_units"] == "1.0" and lanes.version_of(plain, s) != lanes.version_of(on, s)


# needs_model_edge (2026-11-16, user: "lets re-work that concept. because 'always' means nothing"): the renamed switch, its old name still read from a lane file, and no lane's version changed by the rename.
def test_unit_the_switch_says_what_it_does_and_defaults_to_needing_an_edge():
    assert lanes.LaneSpec(name="x", estimator="market", label="x", price=(0.1, 0.9), hours=(0.0, 1.0), max_spread=0.05, min_volume=0.0).needs_model_edge is True
    assert not lanes.wants_bet(spec(needs_model_edge=True, min_edge_room=0.9), 0.50, 0.90, edge(0.50, 0.90))  # the model sees no edge: no bet
    assert lanes.wants_bet(spec(needs_model_edge=False, min_edge_room=0.9), 0.50, 0.90, edge(0.50, 0.90))  # the screen is off


def test_integration_a_lane_file_may_use_the_new_name_or_the_old_one_but_not_both(tmp_path, monkeypatch):
    from kalshi_moonshot import config

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    lane = "[lane.a]\nestimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\n{line}\n"
    (tmp_path / "strategies.toml").write_text(lane.format(line="needs_model_edge=false"))
    new = lanes.lane_specs(config.load())["a"]
    (tmp_path / "strategies.toml").write_text(lane.format(line="always=true"))
    s = config.load()
    old = lanes.lane_specs(s)["a"]
    assert new.needs_model_edge is False and old.needs_model_edge is False
    assert lanes.version_of(new, s) == lanes.version_of(old, s)  # the same lane, whichever name its file uses
    assert lanes.settings_of(old, s)["always"] == "True" and "needs_model_edge" not in lanes.settings_of(old, s)  # reported under the old name, so the rename is not a new version of every lane
    (tmp_path / "strategies.toml").write_text(lane.format(line="always=false"))
    assert lanes.lane_specs(config.load())["a"].needs_model_edge is True
    (tmp_path / "strategies.toml").write_text(lane.format(line="always=true\nneeds_model_edge=false"))
    with pytest.raises(ValueError, match="not both"):
        lanes.lane_specs(config.load())
