"""The PriskP lane's sale is the wave lane's `wave_take` staircase with the PriskP lane's own settings (one concept, 2026-11-22: swing size 1.0, the cushion factor off, no whipsaw switch, studies/results/2026-11-22-priskp-staircase-study.txt)."""
import pytest

from tests.test_wave_top import asker, mem, spot_feed, take_target  # noqa: F401 (mem is a fixture)


def priskp_settings(t):
    t.wave_rev_k, t.wave_rev_p, t.wave_cushion, t.wave_whipsaw = 1.0, 0.5, False, 0.0


def test_unit_there_is_no_high_zone_sale_and_no_lane_setting_for_one(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import lanes, wavetop

    assert "wave_high_zone" not in lanes.LaneSpec.__dataclass_fields__  # removed 2026-11-23 (user: it was never part of the design); a lane file naming it is refused
    assert not hasattr(wavetop, "HIGH")


def test_integration_a_bet_in_the_old_high_zone_is_not_sold_on_the_chance_of_losing(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.50, unit=0.10)
    priskp_settings(t)
    ask = asker(w, t, close)
    spot_feed(close - 2.0, 100.03)  # a cushion of 0.03: the bet could still be lost; the old high-zone sale would have sold here
    assert ask(0.85, 28) is None and ask(0.85, 27) is None


def test_integration_the_priskp_staircase_swing_is_one_typical_minute_scaled_by_the_time_left(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.30, unit=0.20)
    priskp_settings(t)
    ask = asker(w, t, close)
    # 1.0 x 0.20 x sqrt(60 / 60) = 0.20 at a minute left: a 0.06 pullback is no swing, a 0.20 one is
    for bid in (0.36, 0.42, 0.36, 0.42, 0.50):
        ask(bid, 60)
    assert t.wave_last_low == 0.0
    for bid in (0.28, 0.30, 0.50, 0.30, 0.50):  # a pullback of 0.20 confirms the high, a bounce of 0.20 the low at 0.30
        ask(bid, 60)
    assert t.wave_last_low == pytest.approx(0.30)
    why = ask(0.29, 60)
    assert why and why.startswith("wave take") and "last swing low 0.30" in why


def test_integration_the_cushion_factor_off_does_not_scale_the_swing(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.30, unit=0.20)
    priskp_settings(t)
    ask = asker(w, t, close)
    spot_feed(close - 2.0, 102.0)  # far over the target: the factor would be near 2 with the setting on
    for bid in (0.36, 0.42, 0.36, 0.42, 0.50, 0.28, 0.30, 0.50, 0.30, 0.50):
        ask(bid, 60)
    assert t.wave_last_low == pytest.approx(0.30)  # swings of 0.20, not 0.40
