import pytest

from kalshi_moonshot import config, lanes

BASE = """
[lane.fav]
estimator = "ladder"
price = [0.63, 0.89]
hours = [0.02, 0.26]
fixed_stake = 5.0
sides = ["yes", "no"]
"""


def specs(extra):
    return lanes.lane_specs_from(config.load(), BASE + extra)


def test_a_twin_can_override_a_parent_setting():
    out = specs('[lane.twin]\ninherit = "fav"\nprice = [0.45, 0.89]\n')
    assert out["twin"].price == (0.45, 0.89) and out["fav"].price == (0.63, 0.89)


def test_a_missing_parent_or_a_loop_is_an_error():
    with pytest.raises(ValueError, match="not a lane"):
        specs('[lane.twin]\ninherit = "nope"\n')
    with pytest.raises(ValueError, match="loops"):
        specs('[lane.x]\ninherit = "y"\n[lane.y]\ninherit = "x"\n')


def test_paper_lanes_off_switches_off_every_lane_not_named_live(monkeypatch):
    s = config.load()
    toml = "[lane.a]\nestimator='ladder'\nprice=[0.2,0.4]\nhours=[0.0,1.0]\nseries=['KXA15M']\n[lane.b]\nestimator='ladder'\nprice=[0.2,0.4]\nhours=[0.0,1.0]\nseries=['KXB15M']\n"
    on = type(s)(**{**s.__dict__, "live_lanes": frozenset({"a"}), "paper_lanes": True})
    off = type(s)(**{**s.__dict__, "live_lanes": frozenset({"a"}), "paper_lanes": False})
    assert lanes.lane_specs_from(on, toml)["b"].enabled  # the default: a lane that is not live paper-trades
    out = lanes.lane_specs_from(off, toml)
    assert out["a"].enabled and not out["b"].enabled  # the live host: only the live lane runs
    assert "b" in out  # but it is still a lane, so a bet it left open keeps its stop
    every = type(s)(**{**s.__dict__, "live_lanes": None, "paper_lanes": False})
    assert lanes.lane_specs_from(every, toml)["b"].enabled  # an empty LIVE_LANES means every lane is live: nothing to switch off
