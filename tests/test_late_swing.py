"""The late swing lane (lateswing.py): the tier by time left, the buy step's gates and the sale's target (2026-11-20, user)."""
import pytest

from kalshi_moonshot import buychain, config, lanes, lateswing, sellchain, variants
from kalshi_moonshot.strategy import LongShot

TOML = """
[lane.ls]
estimator = "ladder"
needs_model_edge = false
fixed_stake = 5.0
sides = ["yes", "no"]
price = [0.10, 0.90]
hours = [0.0, 0.0334]
series = ["KXBTC15M"]
late_swing_from = 120
late_swing_to = 45
buy_module = "m"

[module.m]
steps = ["price", "late_swing"]
"""


def spec(tmp_path, monkeypatch, extra=""):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(TOML + extra)
    return lanes.lane_specs(config.load())["ls"]


def shot(left=100.0, ask=0.20, side="yes"):
    return LongShot("KXBTC15M-30JAN010015-T1", "t", "", ask - 0.01, ask, left / 3600.0, "2099-01-01T00:00:00Z", left / 3600.0, lane="ls", series="KXBTC15M", side=side, event_ticker="KXBTC15M-30JAN010015")


def ask(sp, sh):
    return buychain.STEPS["late_swing"](buychain.Ctx(config.load(), sp), sh)


def test_unit_the_tier_falls_slice_by_slice_as_time_runs_out():
    got = [lateswing.tier_share(left, 120, 45) for left in (120, 108, 100, 90, 80, 70, 60, 50, 45)]
    assert got == [1.0, 1.0, 0.7, 0.5, 0.4, 0.3, 0.3, 0.2, 0.2]
    assert lateswing.tier_share(121, 120, 45) is None and lateswing.tier_share(44, 120, 45) is None


def test_unit_the_target_is_the_tiers_share_of_the_room():
    assert lateswing.target_gain(0.5, 0.20) == pytest.approx(0.40)
    assert lateswing.target_gain(1.0, 0.20) == pytest.approx(0.80)


def test_unit_the_lane_file_names_the_step_and_its_fields(tmp_path, monkeypatch):
    sp = spec(tmp_path, monkeypatch)
    assert sp.buy_chain == ("price", "late_swing") and sp.late_swing_tiers[0] == 1.0 and "late_swing" in lanes.BUY_STEPS
    assert "swing_take" in sellchain.SELL_STEPS


def test_unit_the_step_buys_a_low_or_high_side_on_a_jumpy_curve_and_nothing_else(tmp_path, monkeypatch):
    sp = spec(tmp_path, monkeypatch)
    monkeypatch.setattr(variants, "snapshot_now", lambda *a, **k: {"crosses": 3, "vol_ratio": 1.4})
    assert ask(sp, shot(ask=0.20)).ok and ask(sp, shot(ask=0.80, side="no")).ok
    assert not ask(sp, shot(ask=0.50)).ok  # the middle is not a low or a high side
    assert not ask(sp, shot(left=130)).ok and not ask(sp, shot(left=40)).ok  # outside the window
    monkeypatch.setattr(variants, "snapshot_now", lambda *a, **k: {"crosses": 1, "vol_ratio": 1.4})
    assert "not jumpy" in ask(sp, shot()).why
    monkeypatch.setattr(variants, "snapshot_now", lambda *a, **k: {"crosses": 3, "vol_ratio": 0.8})
    assert not ask(sp, shot()).ok
    monkeypatch.setattr(variants, "snapshot_now", lambda *a, **k: None)
    assert not ask(sp, shot()).ok  # an unreadable curve is a no


def test_unit_the_stake_is_the_tier_share(tmp_path, monkeypatch):
    sp = spec(tmp_path, monkeypatch)
    assert lateswing.stake_factor(sp, 119) == 1.0 and lateswing.stake_factor(sp, 100) == 0.7 and lateswing.stake_factor(sp, 46) == 0.2
    assert lateswing.stake_factor(spec(tmp_path, monkeypatch, "[lane.other]\nestimator='ladder'\nprice=[0.1,0.9]\nhours=[0,1]\n"), 100) == 0.7


def test_integration_the_sale_waits_for_the_tiers_target(tmp_path, monkeypatch):
    from kalshi_moonshot import curverisk, watcher
    from tests.test_risk_exit_flow import CLOSE, TICKER, held

    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['swing_take']\nlate_swing_from=500\nlate_swing_to=0\nlate_swing_tiers=[0.2]")
    t = w.targets[(TICKER, "risk_only")]
    t.bought_ts = CLOSE - 100.0
    risk = curverisk.Risk(0.3, 0.3, 0.3, "price")
    basis = 0.81 + 0.02
    c = watcher._SellWalk(30.0, basis, 0.83, None)
    assert w._sell_swing_take(t, risk, 0.83, CLOSE - 60, 60.0, 0.0, c) is None  # not at the target
    c = watcher._SellWalk(30.0, basis, 0.97, None)
    why = w._sell_swing_take(t, risk, 0.97, CLOSE - 60, 60.0, 0.17, c)
    assert why and "late swing take" in why and "tier 20%" in why
    t.bought_ts = 0.0
    assert w._sell_swing_take(t, risk, 0.97, CLOSE - 60, 60.0, 0.17, c) is None  # no buy time, no tier
