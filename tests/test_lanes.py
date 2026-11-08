import pytest

from kalshi_moonshot import config, lanes
from kalshi_moonshot.commodities import load_instruments, symbol_for


def settings(tmp_path, **over):
    s = config.load()
    return type(s)(**{**s.__dict__, "data_dir": tmp_path, **over})


def test_builtin_lanes_come_from_settings(tmp_path):
    s = settings(tmp_path, min_price=0.03, max_price=0.12, steady_enabled=False)
    specs = lanes.lane_specs(s)
    assert specs["moonshot"].price == (0.03, 0.12)
    assert specs["steady"].enabled is False
    assert [x.name for x in lanes.enabled_lanes(s)] == ["moonshot", "commodity"]


def test_bet_rule_uses_the_lanes_own_parameters(tmp_path):
    specs = lanes.lane_specs(settings(tmp_path, min_edge=0.05, steady_max_lag=0.15))
    moon, steady = specs["moonshot"], specs["steady"]
    assert lanes.wants_bet(moon, 0.20, 0.10, 0.07)
    assert not lanes.wants_bet(moon, 0.12, 0.10, 0.01)
    assert lanes.wants_bet(steady, 0.55, 0.66, -0.2)  # the lag rule ignores edge
    assert not lanes.wants_bet(steady, 0.25, 0.70, 0.0)




def test_a_toml_file_changes_a_lane_and_adds_a_new_one(tmp_path):
    (tmp_path / "strategies.toml").write_text(
        """
[lane.moonshot]
price = [0.01, 0.04]
min_edge = 0.12

[lane.metals]
estimator = "ladder"
price = [0.02, 0.30]
hours = [1, 48]
min_edge = 0.10
series = ["KXGOLDD", "KXSILVERD"]
needs_instrument = true
"""
    )
    specs = lanes.lane_specs(settings(tmp_path))
    assert specs["moonshot"].price == (0.01, 0.04)
    assert specs["moonshot"].min_edge == 0.12
    assert specs["moonshot"].shortlist == 15  # untouched fields keep their value
    assert specs["metals"].series == ("KXGOLDD", "KXSILVERD")
    assert specs["metals"].estimator == "ladder"


def test_a_typo_in_the_toml_fails_loudly(tmp_path):
    (tmp_path / "strategies.toml").write_text('[lane.moonshot]\nmin_edgee = 0.1\n')
    with pytest.raises(ValueError, match="unknown field"):
        lanes.lane_specs(settings(tmp_path))
    (tmp_path / "strategies.toml").write_text('[lane.fresh]\nprice = [0.1, 0.2]\n')
    with pytest.raises(ValueError, match="new lanes need"):
        lanes.lane_specs(settings(tmp_path))


def test_editing_the_file_applies_without_a_restart(tmp_path):
    s = settings(tmp_path)
    assert lanes.lane_specs(s)["moonshot"].min_edge == s.min_edge
    (tmp_path / "strategies.toml").write_text("[lane.moonshot]\nmin_edge = 0.2\n")
    assert lanes.lane_specs(s)["moonshot"].min_edge == 0.2


def test_instruments_are_data_and_overridable(tmp_path):
    assert symbol_for("KXWTI") == "hl:xyz:CL"  # daily WTI has no suffix
    assert symbol_for("KXGOLDD") == "hl:xyz:GOLD"
    assert symbol_for("KXGOLDMON") is None
    (tmp_path / "instruments.toml").write_text(
        '[[instrument]]\nprefix = "KXCORN"\nsymbol = "ZC=F"\nsuffixes = ["W"]\n'
    )
    custom = load_instruments(tmp_path)
    assert symbol_for("KXCORNW", custom) == "ZC=F"
    assert symbol_for("KXGOLDD", custom) is None


def test_a_lane_whose_only_exit_is_the_adaptive_shock_or_cross_is_watched():
    import dataclasses

    from kalshi_moonshot.lanes import LaneSpec, takes_profit

    base = LaneSpec(name="x", estimator="e", label="l", price=(0.2, 0.7), hours=(0.0, 1.0), max_spread=0.05, min_volume=0)
    assert not takes_profit(base)
    assert takes_profit(dataclasses.replace(base, shock_units=1.0))  # shock_units replaced shock_bp on crypto_multiscale on 11-06; the watcher skipped lanes with no other exit
    assert takes_profit(dataclasses.replace(base, cross_units=0.0))
