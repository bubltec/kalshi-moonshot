"""The PriskP lane through its lane file (the block in data/strategies.toml, copied here so the test does not read the untracked file): it loads, its module and exit resolve, the buy chain is the one step, the sale is the staircase, and the lane carries no gate the user did not ask for."""
from pathlib import Path

import pytest

from kalshi_moonshot import config, lanes, priskp

BLOCK = """
[lane.priskp]
enabled = true
label = "PriskP"
estimator = "market"
numeric = "allow"
one_per = "none"
series = ["KXBTC15M", "KXETH15M"]
sides = ["yes", "no"]
timed_suffix = "15M"
shadow_suffix = "15M"
price = [0.30, 0.85]
hours = [0.0, 0.26]
max_spread = 1.0
min_volume = 0
buy_module = "priskp"
buy_rest_seconds = 5
buy_rest_follow = true
buy_rest_avg_seconds = 5
buy_rest_below = 0.02
exit_module = "priskp_exit"
wave_rev_k = 1.0
wave_rev_p = 0.5
wave_cushion = false
wave_high_zone = false
wave_whipsaw_range = 0.0
priority = 45
fixed_stake = 5.0
paper_fill_model = true

[module.priskp]
label = "the PriskP checks"
steps = ["price", "priskp"]

[exit.priskp_exit]
label = "the PriskP sale"
steps = ["wave_take"]
curve_risk_live = true
curve_risk_spot_weight = 0.0

[exit.priskp_exit.profit]
curve_take = true

[exit.priskp_exit.loss]
"""


@pytest.fixture
def spec(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    Path(tmp_path, "strategies.toml").write_text(BLOCK)
    s = config.load()
    return s, lanes.lane_specs(s)["priskp"]


def test_the_lane_loads_with_the_one_buy_step_and_the_staircase_sale(spec):
    _s, sp = spec
    assert sp.enabled and sp.buy_chain == ("price", "priskp") and sp.sell_chain == ("wave_take",)
    assert sp.buy_module == "priskp" and sp.exit_module == "priskp_exit"
    assert sp.buy_rest_follow and sp.buy_rest_seconds == 5 and sp.buy_rest_below == 0.02 and sp.buy_rest_avg_seconds == 5  # the resting buy follows the ask down (user, 2026-11-22)
    assert (sp.wave_rev_k, sp.wave_cushion, sp.wave_high_zone, sp.wave_whipsaw_range) == (1.0, False, False, 0.0)  # the wave lane's staircase, set for PriskP: one concept


def test_the_lane_has_no_gate_the_user_did_not_ask_for(spec):
    _s, sp = spec
    assert sp.price == (0.30, 0.85) and sp.max_spread >= 1.0 and sp.min_volume == 0  # the price band the user asked for (0.30 to 0.85, as the `price` step), no spread limit, no volume limit: the step's list is everything that can turn a buy down
    assert sp.buy_chain == ("price", "priskp")  # spread, entry_window, time_left, cooldown, blackout and the rest are not applied


def test_the_watcher_builds_the_cutoffs_only_when_a_lane_names_the_step(spec, tmp_path):
    _s, sp = spec
    assert priskp.wanted({"priskp": sp}) is True
    off = {"x": type(sp)(**{**sp.__dict__, "enabled": False})}
    assert priskp.wanted(off) is False


def test_the_cutoffs_file_is_read_into_memory_when_it_changes(spec, tmp_path):
    import json
    import time

    s, _sp = spec
    priskp.clear()
    priskp._checked_at, priskp._file_mtime = 0.0, 0.0
    assert priskp.refresh(s, time.time()) == 0  # no file yet: nothing loaded, no error
    (tmp_path / "priskp_cutoffs.json").write_text(json.dumps({"built_at": 1.0, "cuts": {"KXBTC15M|1|cushion": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]}}))
    assert priskp.refresh(s, time.time() + 120.0) == 1
    assert priskp.fifths("KXBTC15M", 200.0, {"cushion": 4.5})["cushion"] == 5
    priskp.clear()
