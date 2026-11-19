"""The sell chain: the risk exit's decisions as named steps a lane asks in its own order (sellchain.py, lane field sell_chain, an exit module's `steps`).

2026-11-18, user, with the buy already a chain of named steps: "we also need to build a sell module, since we're refactoring this flow". A lane with no chain is asked the default order, the one the risk exit always used, and sells with the same reasons (tests/test_risk_exit_flow.py, test_curverisk.py, test_risk_line_switch.py and test_exit_module.py pass unchanged). These tests cover a lane that names a chain: its check, an exit module's steps reaching the lane and the watcher, and what leaving a step out or reordering does to a real held bet's sale signals."""
import pytest

from kalshi_moonshot import sellchain
from kalshi_moonshot import ticks as ticks_mod
from tests.test_buychain import BASE, load
from tests.test_risk_exit_flow import (
    TRAIL,
    climb,
    held,
    level,
    run_up,
    tick,
    with_hour_view,
)

FULL = "['never_ahead','under_even','profit_take','trail','long_hold']"


@pytest.fixture(autouse=True)
def _clean_mids():
    ticks_mod.mids._rows.clear()
    yield
    ticks_mod.mids._rows.clear()


def bet(tmp_path, monkeypatch, name, **kw):
    """A held bet of its own (its own data folder and an empty quote history), so two can be compared in one test."""
    (tmp_path / name).mkdir()
    ticks_mod.mids._rows.clear()
    return held(tmp_path / name, monkeypatch, live=True, **kw)


def first_sale(w, bid, left, limit=200):
    """The bid sits at `bid` from `left` seconds to the close, one quote a second: (seconds until the sale, its reason), or None."""
    for k in range(limit):
        tick_out = tick(w, bid, left - k)
        if tick_out:
            return k, tick_out[0].why
    return None


def test_unit_a_chain_names_known_steps_each_at_most_once():
    sellchain.check((), "lane 'a'")
    sellchain.check(("trail", "late_tight"), "lane 'a'")
    with pytest.raises(ValueError, match="lane 'a': sell_chain names \\['stop'\\]"):
        sellchain.check(("stop", "late_tight"), "lane 'a'")
    with pytest.raises(ValueError, match="a step twice"):
        sellchain.check(("late_tight", "trail", "late_tight"), "lane 'a'")


# ---- unit: the lane field and an exit module's steps ----
STEPS = "[exit.round_trip]\nsteps=['profit_take','trail','long_hold']\n[exit.round_trip.profit]\ncurve_take=true\n[exit.round_trip.loss]\ncurve_risk_sell=0.04\n\n"
PLAIN = "[exit.plain]\n[exit.plain.profit]\ncurve_take=true\n[exit.plain.loss]\ncurve_risk_sell=0.04\n\n"


def test_unit_a_lane_writes_its_own_chain_and_a_lane_without_one_has_none(tmp_path, monkeypatch):
    _, got = load(tmp_path, monkeypatch, f"[lane.a]\n{BASE}sell_chain=['trail','late_tight']\n\n[lane.b]\n{BASE}")
    assert got["a"].sell_chain == ("trail", "late_tight") and got["b"].sell_chain == ()


def test_integration_a_chain_without_profit_take_and_trail_does_not_take_a_gain(tmp_path, monkeypatch):
    plain = bet(tmp_path, monkeypatch, "plain", take="true")
    assert climb(plain, 0.82, 0.92, 700) is None and level(plain, 0.92, 700) is not None  # the default order takes it
    w = bet(tmp_path, monkeypatch, "no_take", take="true", extra="sell_chain=['never_ahead','under_even','long_hold']")
    assert climb(w, 0.82, 0.92, 700) is None and level(w, 0.92, 700) is None  # the same gain, the same lane settings: nothing asks for it


def test_integration_the_trail_alone_in_a_chain_still_trails_and_the_note_names_it(tmp_path, monkeypatch):
    w = bet(tmp_path, monkeypatch, "trail", take="true", extra="curve_take_trail=1.0\ncurve_take_trail_live=true\nsell_chain=['trail']")
    assert run_up(w) is None  # under the bar, still rising
    assert tick(w, 0.89, 250) == []  # one cent off the high: inside the market's own swing
    out = tick(w, 0.86, 249)
    assert out and out[0].why.startswith(TRAIL) and "0.90 high" in out[0].why  # the high was kept with no profit_take step in the chain
    assert out[0].why.endswith(" [sell chain lane: trail | sold at trail]")


def test_integration_the_trail_alone_does_not_take_a_gain_that_only_the_profit_take_would(tmp_path, monkeypatch):
    w = bet(tmp_path, monkeypatch, "trail", take="true", extra="curve_take_trail=1.0\ncurve_take_trail_live=true\nsell_chain=['trail']")
    assert climb(w, 0.82, 0.92, 700) is None and level(w, 0.92, 700) is None  # over the bar and flat, but never off its high: the trail has nothing to say


UE = "curve_risk_under_even=0.1\ncurve_risk_under_even_live=true"


def _favorite_under_even():
    from tests.test_risk_exit_flow import flat

    return flat(0.78, 445, 305) + [(left, 0.45) for left in range(300, 200, -1)]


def test_integration_the_long_hold_is_a_hold_only_for_the_steps_after_it(tmp_path, monkeypatch):
    from tests.test_risk_exit_flow import quotes

    with_hour_view(monkeypatch, 0.55)  # the long layer says the side is likely to pay: over the lane's 0.4 with seven minutes left
    first = quotes(bet(tmp_path, monkeypatch, "under_even_first", confirm=0.4, extra=f"sell_chain=['under_even','long_hold']\n{UE}"), _favorite_under_even())
    assert first and first[0].why.startswith("curve risk exit (under even)") and first[0].why.endswith("| sold at under_even]")  # asked first, the under-even step sells before the hold is reached
    held_first = quotes(bet(tmp_path, monkeypatch, "hold_first", confirm=0.4, extra=f"sell_chain=['long_hold','under_even']\n{UE}"), _favorite_under_even())
    assert held_first == []  # the hold comes first: no step after it is asked
    no_hold = quotes(bet(tmp_path, monkeypatch, "no_hold", confirm=0.4, extra=f"sell_chain=['under_even']\n{UE}"), _favorite_under_even())
    assert no_hold and no_hold[0].why.startswith("curve risk exit (under even)")  # left out, the hold is not asked
