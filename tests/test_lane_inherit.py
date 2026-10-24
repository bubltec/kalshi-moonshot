import pytest

from kalshi_moonshot import config, lanes

BASE = """
[lane.fav]
estimator = "ladder"
price = [0.63, 0.89]
hours = [0.02, 0.26]
fixed_stake = 5.0
sides = ["yes", "no"]
stop_loss = true
"""


def specs(extra):
    return lanes.lane_specs_from(config.load(), BASE + extra)


def test_a_twin_takes_the_parents_settings_and_changes_only_what_it_names():
    out = specs('[lane.twin]\ninherit = "fav"\nhover_dip = 0.5\n')
    assert out["twin"].price == (0.63, 0.89) and out["twin"].fixed_stake == 5.0 and out["twin"].stop_loss is True
    assert out["twin"].hover_dip == 0.5 and out["fav"].hover_dip == 0.0
    assert out["twin"].name == "twin"


def test_a_twin_can_override_a_parent_setting():
    out = specs('[lane.twin]\ninherit = "fav"\nprice = [0.45, 0.89]\n')
    assert out["twin"].price == (0.45, 0.89) and out["fav"].price == (0.63, 0.89)


def test_chains_work_and_a_change_to_the_parent_reaches_every_twin():
    out = specs('[lane.a]\ninherit = "fav"\nhover_dip = 0.5\n[lane.b]\ninherit = "a"\nstop_guard = true\n')
    assert out["b"].hover_dip == 0.5 and out["b"].stop_guard is True and out["b"].price == (0.63, 0.89)
    out2 = lanes.lane_specs_from(config.load(), (BASE + '[lane.a]\ninherit = "fav"\n').replace("0.63, 0.89", "0.60, 0.85"))
    assert out2["a"].price == (0.60, 0.85)


def test_a_missing_parent_or_a_loop_is_an_error():
    with pytest.raises(ValueError, match="not a lane"):
        specs('[lane.twin]\ninherit = "nope"\n')
    with pytest.raises(ValueError, match="loops"):
        specs('[lane.x]\ninherit = "y"\n[lane.y]\ninherit = "x"\n')
