"""The hour's width correction scales a nudge's cap (2026-11-15, user: make the hourly factor affect the nudge amount)."""
import pytest

from kalshi_moonshot import widthfit
from kalshi_moonshot.lanes import nudge


def test_unit_the_scale_keeps_the_whole_cap_at_k_of_one_or_under_and_shrinks_above_it_to_half():
    assert widthfit.nudge_scale(0.5) == 1.0
    assert widthfit.nudge_scale(1.0) == 1.0
    assert widthfit.nudge_scale(1.25) == pytest.approx(0.8)
    assert widthfit.nudge_scale(2.0) == 0.5
    assert widthfit.nudge_scale(3.0) == 0.5  # the floor: a wild hour halves the nudge at most


def test_unit_hour_k_is_the_last_fit_while_fresh_and_one_when_there_is_none_or_it_is_stale(monkeypatch):
    monkeypatch.setattr(widthfit, "latest", None)
    assert widthfit.hour_k(1000.0) == 1.0
    monkeypatch.setattr(widthfit, "latest", (1.6, 1000.0, 50))
    assert widthfit.hour_k(1000.0 + 600.0) == 1.6
    assert widthfit.hour_k(1000.0 + widthfit.MAX_AGE + 1.0) == 1.0


def test_unit_the_nudge_takes_the_scale_and_never_exceeds_its_cap():
    assert nudge(0.02, 1.0, 1.0) == 0.02
    assert nudge(0.02, 1.0, 1.0, 0.5) == pytest.approx(0.01)
    assert nudge(0.02, 1.0, 1.0, 5.0) == 0.02  # a scale over 1 is not a way past the cap
    assert nudge(0.02, 0.5, 0.5, 0.5) == pytest.approx(0.0025)


def test_unit_the_nudge_in_units_is_the_markets_typical_move_times_everything_that_agrees():
    from kalshi_moonshot.lanes import nudge_in_units

    assert nudge_in_units(1.0, 0.08, 1.0, 1.0) == pytest.approx(0.08)  # no cap: a whole typical move at full agreement
    assert nudge_in_units(1.0, 0.08, 0.5, 0.5, 0.5) == pytest.approx(0.01)
    assert nudge_in_units(1.0, 0.16, 1.0, 1.0) == pytest.approx(2 * nudge_in_units(1.0, 0.08, 1.0, 1.0))  # a wilder market allows a bigger miss
    assert nudge_in_units(1.0, None, 1.0, 1.0) == 0.0 and nudge_in_units(0.0, 0.08, 1.0, 1.0) == 0.0
    assert nudge_in_units(1.0, 0.08, 0.0, 1.0) == 0.0  # no agreement from the history, no nudge
