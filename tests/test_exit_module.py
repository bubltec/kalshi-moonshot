"""The exit module: a named exit in strategies.toml, in a profit part and a loss part, that lanes are assigned to (lane field exit_module, lanes._with_exit).

2026-11-18, user, on the paper lane crypto_risk_score still carrying 54 settings, 18 of them its exit: "we need a sell module which covers profit and loss"; "you mentioned it above, exit module ... do that"."""
import pytest

from kalshi_moonshot import lanes
from tests.test_buychain import BASE, MODULES, load
from tests.test_risk_exit_flow import held, sold_after

EXIT = """[exit.risk_exit]
label='a gain is taken when the curve says so, a loss is sold under the line'
curve_risk_live=true
curve_wait_share=0.25

[exit.risk_exit.profit]
curve_take=true
curve_risk_min_profit=0.15

[exit.risk_exit.loss]
curve_risk_sell=0.10
curve_risk_max_loss=0
stop_loss=false

"""
INLINE = "curve_risk_live=true\ncurve_wait_share=0.25\ncurve_take=true\ncurve_risk_min_profit=0.15\ncurve_risk_sell=0.10\ncurve_risk_max_loss=0\nstop_loss=false\n"


def test_unit_a_setting_belongs_to_the_profit_part_the_loss_part_or_neither():
    assert lanes.exit_part("curve_take") == "profit" and lanes.exit_part("curve_take_sure_late") == "profit" and lanes.exit_part("curve_risk_min_profit") == "profit"
    assert lanes.exit_part("curve_risk_sell") == "loss" and lanes.exit_part("stop_loss") == "loss" and lanes.exit_part("curve_risk_early_hold") == "loss"
    assert lanes.exit_part("price") is None and lanes.exit_part("risk_score_buy") is None and lanes.exit_part("not_a_setting") is None


def test_unit_a_lane_on_an_exit_module_is_the_same_lane_as_one_that_writes_the_settings_itself(tmp_path, monkeypatch):
    s, got = load(tmp_path, monkeypatch, EXIT + f"[lane.a]\n{BASE}exit_module='risk_exit'\n\n[lane.b]\n{BASE}{INLINE}")
    a, b = got["a"], got["b"]
    assert (a.curve_take, a.curve_risk_min_profit, a.curve_risk_sell, a.curve_risk_max_loss, a.stop_loss, a.curve_risk_live, a.curve_wait_share) == (True, 0.15, 0.10, 0, False, True, 0.25)
    assert a.exit_module == "risk_exit" and b.exit_module == ""
    assert lanes.settings_of(a, s) == lanes.settings_of(b, s)  # the module's name is not what the lane does; its settings are


def test_unit_the_lanes_own_setting_wins_and_two_lanes_share_one_module(tmp_path, monkeypatch):
    _, got = load(tmp_path, monkeypatch, EXIT + f"[lane.a]\n{BASE}exit_module='risk_exit'\n\n[lane.b]\n{BASE}exit_module='risk_exit'\ncurve_risk_sell=0.25\n\n[lane.c]\n{BASE}")
    assert got["a"].curve_risk_sell == 0.10 and got["b"].curve_risk_sell == 0.25 and got["b"].curve_take is True
    assert got["c"].exit_module == "" and got["c"].curve_take is False  # a lane with no module is as it was


def test_unit_a_lane_takes_a_buy_module_and_an_exit_module_together_and_through_inherit(tmp_path, monkeypatch):
    _, got = load(tmp_path, monkeypatch, MODULES + EXIT + f"[lane.a]\n{BASE}buy_module='risk_first'\nexit_module='risk_exit'\n\n[lane.twin]\ninherit='a'\nfixed_stake=2.0\n")
    for name in ("a", "twin"):
        assert got[name].buy_chain == ("risk_score", "price", "ladder") and got[name].curve_risk_sell == 0.10 and got[name].exit_module == "risk_exit"


@pytest.mark.parametrize("text,word", [
    (EXIT + f"[lane.a]\n{BASE}exit_module='nope'\n", "not an exit module"),
    (f"[exit.half]\n[exit.half.profit]\ncurve_take=true\n\n[lane.a]\n{BASE}exit_module='half'\n", "covers profit and loss"),
    (f"[exit.bad]\n[exit.bad.profit]\ncurve_risk_sell=0.1\n[exit.bad.loss]\n\n[lane.a]\n{BASE}exit_module='bad'\n", "is a loss setting, written in the profit part"),
    (f"[exit.bad]\n[exit.bad.profit]\n[exit.bad.loss]\ncurve_take=true\n\n[lane.a]\n{BASE}exit_module='bad'\n", "is a profit setting, written in the loss part"),
    (f"[exit.bad]\nrisk_score_buy=0.25\n[exit.bad.profit]\n[exit.bad.loss]\n\n[lane.a]\n{BASE}exit_module='bad'\n", "not an exit setting"),
    (f"[exit.bad]\n[exit.bad.profit]\n[exit.bad.loss]\nprice=[0.1,0.9]\n\n[lane.a]\n{BASE}exit_module='bad'\n", "not an exit setting"),
    (f"[exit.bad]\ncurve_risk_sell=0.2\n[exit.bad.profit]\n[exit.bad.loss]\ncurve_risk_sell=0.1\n\n[lane.a]\n{BASE}exit_module='bad'\n", "written twice"),
])
def test_unit_a_lane_file_with_a_bad_exit_module_does_not_load(tmp_path, monkeypatch, text, word):
    with pytest.raises(ValueError, match=word):
        load(tmp_path, monkeypatch, text)


def test_unit_an_exit_module_with_empty_parts_changes_nothing(tmp_path, monkeypatch):
    s, got = load(tmp_path, monkeypatch, f"[exit.plain]\n[exit.plain.profit]\n[exit.plain.loss]\n\n[lane.a]\n{BASE}exit_module='plain'\n\n[lane.b]\n{BASE}")
    assert lanes.settings_of(got["a"], s) == lanes.settings_of(got["b"], s)


# Through the lane file, the watcher and its sale signals (tests/test_risk_exit_flow.py's held bet): the module's loss part decides the sale.
QUIET = "exit_module='quiet'\n\n[exit.quiet]\n[exit.quiet.profit]\n[exit.quiet.loss]\ncurve_risk_line_live=false"

