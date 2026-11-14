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


# ---- the stretch on the buy bar (2026-11-14, user: "if something misses by a cent or two ... it skips to the history read or the trending read") ----

def test_unit_edge_miss_is_the_shortfall_under_the_scaled_bar_only():
    sp = spec(min_edge_room=0.10)
    e = edge(0.629, 0.58)
    room = 1.0 - 0.58 - fee_per_contract(0.58)
    assert lanes.edge_miss(sp, 0.629, 0.58, e) == pytest.approx(0.10 * room - e)  # the BTC YES of 13:03: about 0.008
    assert lanes.edge_miss(sp, 0.70, 0.58, edge(0.70, 0.58)) < 0  # over the bar: no miss
    assert lanes.edge_miss(spec(min_edge=0.03), 0.629, 0.58, e) is None  # no scaled bar
    assert lanes.edge_miss(spec(min_edge=0.05, min_edge_room=0.10), 0.629, 0.58, e) is None  # the flat bar fails too: not reached for
    assert lanes.edge_miss(spec(always=True, min_edge_room=0.10), 0.629, 0.58, e) is None


def test_unit_stretch_ok_needs_a_miss_inside_the_noise_and_the_trend_its_way():
    assert lanes.stretch_ok(0.008, 0.02, 1.5, 1.0) is True
    assert lanes.stretch_ok(0.008, 0.02, 0.5, 1.0) is False  # the side has not really moved its way
    assert lanes.stretch_ok(0.008, 0.02, -2.0, 1.0) is False  # trending against
    assert lanes.stretch_ok(0.030, 0.02, 1.5, 1.0) is False  # a clear miss
    assert lanes.stretch_ok(0.008, 0.02, 3.5, 1.0) is False  # over three typical moves: an anomaly, no rule applies
    assert lanes.stretch_ok(0.008, 0.02, None, 1.0) is False  # no reading of the trend
    assert lanes.stretch_ok(None, 0.02, 1.5, 1.0) is False
    assert lanes.stretch_ok(-0.01, 0.02, 1.5, 1.0) is False  # not a miss


STRETCH = "min_edge_room=0.10\nrecord_adjust=false\nstretch_seconds=5\nsqueeze_seconds=5"


@pytest.fixture(autouse=True)
def _unit(monkeypatch):
    monkeypatch.setattr(runner, "market_unit", lambda ticker, now: 0.06)  # a typical minute of 0.06: 0.017 of noise over 5 s, the ruler the squeeze uses too


def test_integration_a_near_miss_with_the_side_trending_its_way_is_bet_and_tagged(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "side_trend_read", lambda s, shot, seconds, now=None: (0.09, 0.06))  # +1.5 typical moves
    got = decide(tmp_path, monkeypatch, STRETCH, 0.58, 0.629)  # 0.008 under the bar
    assert got.action == "bet" and "[stretch buy" in got.note


def test_integration_the_same_near_miss_trending_against_is_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "side_trend_read", lambda s, shot, seconds, now=None: (-0.09, 0.06))
    got = decide(tmp_path, monkeypatch, STRETCH, 0.58, 0.629)
    assert got.action == "pass" and "[stretch buy" not in got.note


def test_integration_a_clear_miss_is_not_stretched_even_trending_its_way(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "side_trend_read", lambda s, shot, seconds, now=None: (0.09, 0.06))
    assert decide(tmp_path, monkeypatch, STRETCH, 0.58, 0.60).action == "pass"  # about 0.04 under the bar, over twice the noise


def test_integration_with_the_stretch_off_the_near_miss_is_passed(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "side_trend_read", lambda s, shot, seconds, now=None: (0.09, 0.06))
    assert decide(tmp_path, monkeypatch, "min_edge_room=0.10\nrecord_adjust=false", 0.58, 0.629).action == "pass"


def test_integration_with_no_trend_reading_nothing_is_stretched(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "side_trend_read", lambda s, shot, seconds, now=None: None)
    assert decide(tmp_path, monkeypatch, STRETCH, 0.58, 0.629).action == "pass"


def test_integration_the_ladder_screen_keeps_a_stretched_near_miss_and_drops_it_otherwise(tmp_path, monkeypatch):
    from kalshi_moonshot.estimators import LadderEstimator

    class Pricer:
        def price_markets(self, markets, s, now):
            return {m["ticker"]: (0.629, "note") for m in markets}

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    sp = dataclasses.replace(spec(min_edge_room=0.10, stretch_seconds=5.0, squeeze_seconds=5.0), price=(0.30, 0.90))
    sh = LongShot("KXBTCD-X-T1", "t", "r", 0.57, 0.58, 100.0, "2099-01-01T00:00:00Z", 0.2, lane="x", series="KXBTCD", event_ticker="KXBTCD-X", side="yes")
    rows = [{"ticker": "KXBTCD-X-T1", "event_ticker": "KXBTCD-X"}]
    monkeypatch.setattr(runner, "side_trend_read", lambda s, shot, seconds, now=None: (0.09, 0.06))
    assert [k.ticker for k in LadderEstimator(Pricer()).screen([sh], rows, sp, s, None)] == ["KXBTCD-X-T1"]
    monkeypatch.setattr(runner, "side_trend_read", lambda s, shot, seconds, now=None: (-0.09, 0.06))
    est = LadderEstimator(Pricer())
    assert est.screen([sh], rows, sp, s, None) == [] and len(est.dropped) == 1


def test_unit_the_stretch_changes_a_lanes_version_only_when_it_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    plain = spec(min_edge_room=0.10)
    assert "stretch_seconds" not in lanes.settings_of(plain, s)
    on = dataclasses.replace(plain, stretch_seconds=5.0)
    assert lanes.settings_of(on, s)["stretch_seconds"] == "5.0" and lanes.version_of(plain, s) != lanes.version_of(on, s)


# ---- the stretch and the squeeze work as a pair (2026-11-14, user: "The stretch buy and squeeze sale need to work together") ----
def test_integration_a_lane_with_the_stretch_but_no_squeeze_gets_no_stretch(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "side_trend_read", lambda s, shot, seconds, now=None: (0.09, 0.06))
    alone = decide(tmp_path, monkeypatch, "min_edge_room=0.10\nrecord_adjust=false\nstretch_seconds=5", 0.58, 0.629)
    assert alone.action == "pass" and "[stretch buy" not in alone.note


def test_integration_the_buy_is_measured_with_the_squeezes_ruler_not_the_trend_readers(tmp_path, monkeypatch):
    # the side's 60 s moves are wild (sd 0.20: the old ruler allowed 0.058), the market's typical minute is quiet (0.02: 0.006 over 5 s). The miss of 0.008 is not near
    monkeypatch.setattr(runner, "side_trend_read", lambda s, shot, seconds, now=None: (0.30, 0.20))
    monkeypatch.setattr(runner, "market_unit", lambda ticker, now: 0.02)
    assert decide(tmp_path, monkeypatch, STRETCH, 0.58, 0.629).action == "pass"
    monkeypatch.setattr(runner, "market_unit", lambda ticker, now: 0.06)
    assert decide(tmp_path, monkeypatch, STRETCH, 0.58, 0.629).action == "bet"


def test_integration_with_no_reading_of_the_markets_unit_nothing_is_stretched(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "side_trend_read", lambda s, shot, seconds, now=None: (0.09, 0.06))
    monkeypatch.setattr(runner, "market_unit", lambda ticker, now: None)
    assert decide(tmp_path, monkeypatch, STRETCH, 0.58, 0.629).action == "pass"
