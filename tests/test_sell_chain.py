"""The sell chain: the risk exit's decisions as named steps a lane asks in its own order (sellchain.py, lane field sell_chain, an exit module's `steps`).

2026-11-18, user, with the buy already a chain of named steps: "we also need to build a sell module, since we're refactoring this flow". A lane with no chain is asked the default order, the one the risk exit always used, and sells with the same reasons (tests/test_risk_exit_flow.py, test_curverisk.py, test_risk_line_switch.py and test_exit_module.py pass unchanged). These tests cover a lane that names a chain: its check, an exit module's steps reaching the lane and the watcher, and what leaving a step out or reordering does to a real held bet's sale signals."""
import pytest

from kalshi_moonshot import lanes, sellchain, watcher
from kalshi_moonshot import ticks as ticks_mod
from tests.test_buychain import BASE, load
from tests.test_risk_exit_flow import (
    TRAIL,
    climb,
    held,
    level,
    run_up,
    signals,
    slide_at,
    sold_after,
    tick,
    with_hour_view,
)

FULL = "['never_ahead','under_even','profit_take','trail','long_hold','loss']"


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


# ---- unit: the names, the check, the note ----
def test_unit_the_default_order_is_the_risk_exits_order_and_the_watcher_has_a_method_for_each_step():
    assert sellchain.DEFAULT == ("never_ahead", "under_even", "profit_take", "trail", "long_hold", "loss")
    assert sellchain.order_of(()) == sellchain.DEFAULT and sellchain.order_of(None) == sellchain.DEFAULT
    assert sellchain.order_of(["loss", "trail"]) == ("loss", "trail")
    assert set(watcher._SELL) == set(sellchain.SELL_STEPS) and all(callable(f) for f in watcher._SELL.values())


def test_unit_a_chain_names_known_steps_each_at_most_once():
    sellchain.check((), "lane 'a'")
    sellchain.check(("trail", "loss"), "lane 'a'")
    with pytest.raises(ValueError, match="lane 'a': sell_chain names \\['stop'\\]"):
        sellchain.check(("stop", "loss"), "lane 'a'")
    with pytest.raises(ValueError, match="a step twice"):
        sellchain.check(("loss", "trail", "loss"), "lane 'a'")


def test_unit_the_note_is_only_added_at_the_end_and_survives_the_stores_cut():
    assert sellchain.tag(("trail", "loss"), "", "loss") == " [sell chain lane: trail > loss | sold at loss]"
    assert sellchain.tag(("trail", "loss"), "round_trip", "trail") == " [sell chain round_trip: trail > loss | sold at trail]"
    short = sellchain.tagged("curve risk exit (max loss): x", ("loss",), "", "loss")
    assert short == "curve risk exit (max loss): x [sell chain lane: loss | sold at loss]"
    long = sellchain.tagged("curve risk exit (profit take): " + "y" * 1200, sellchain.DEFAULT, "risk_exit", "profit_take")
    assert len(long) == sellchain.KEEP and long.startswith("curve risk exit (profit take): yyy") and long.endswith("| sold at profit_take]")


# ---- unit: the lane field and an exit module's steps ----
STEPS = "[exit.round_trip]\nsteps=['profit_take','trail','long_hold']\n[exit.round_trip.profit]\ncurve_take=true\n[exit.round_trip.loss]\ncurve_risk_sell=0.04\n\n"
PLAIN = "[exit.plain]\n[exit.plain.profit]\ncurve_take=true\n[exit.plain.loss]\ncurve_risk_sell=0.04\n\n"


def test_unit_a_lane_writes_its_own_chain_and_a_lane_without_one_has_none(tmp_path, monkeypatch):
    _, got = load(tmp_path, monkeypatch, f"[lane.a]\n{BASE}sell_chain=['trail','loss']\n\n[lane.b]\n{BASE}")
    assert got["a"].sell_chain == ("trail", "loss") and got["b"].sell_chain == ()


def test_unit_an_exit_modules_steps_become_the_lanes_chain_also_through_inherit(tmp_path, monkeypatch):
    _, got = load(tmp_path, monkeypatch, STEPS + PLAIN + f"[lane.a]\n{BASE}exit_module='round_trip'\n\n[lane.twin]\ninherit='a'\nfixed_stake=2.0\n\n[lane.c]\n{BASE}exit_module='plain'\n\n[lane.d]\n{BASE}exit_module='plain'\nsell_chain=['loss']\n")
    assert got["a"].sell_chain == ("profit_take", "trail", "long_hold") and got["twin"].sell_chain == got["a"].sell_chain
    assert got["a"].curve_take is True and got["a"].curve_risk_sell == 0.04  # the module's settings still reach the lane
    assert got["c"].sell_chain == ()  # a module with no steps leaves the lane on the default order
    assert got["d"].sell_chain == ("loss",)  # and such a lane may write its own


@pytest.mark.parametrize("text,word", [
    (f"[lane.a]\n{BASE}sell_chain=['loss','stop']\n", "sell_chain names"),
    (f"[lane.a]\n{BASE}sell_chain=['loss','loss']\n", "a step twice"),
    (STEPS + f"[lane.a]\n{BASE}exit_module='round_trip'\nsell_chain=['loss']\n", "writes sell_chain too"),
    (f"[exit.bad]\nsteps=['loss','ladder']\n[exit.bad.profit]\n[exit.bad.loss]\n\n[lane.a]\n{BASE}exit_module='bad'\n", "exit module 'bad': sell_chain names"),
    (f"[exit.bad]\nsteps=[]\n[exit.bad.profit]\n[exit.bad.loss]\n\n[lane.a]\n{BASE}exit_module='bad'\n", "`steps` is empty"),
    (f"[exit.bad]\n[exit.bad.profit]\n[exit.bad.loss]\nsteps=['loss']\n\n[lane.a]\n{BASE}exit_module='bad'\n", "not an exit setting"),  # steps belong at the module's top, not in a part
])
def test_unit_a_lane_file_with_a_bad_sell_chain_does_not_load(tmp_path, monkeypatch, text, word):
    with pytest.raises(ValueError, match=word):
        load(tmp_path, monkeypatch, text)


def test_unit_a_chain_is_part_of_the_lanes_version_only_when_it_is_set(tmp_path, monkeypatch):
    s, got = load(tmp_path, monkeypatch, STEPS + PLAIN + f"[lane.a]\n{BASE}exit_module='plain'\n\n[lane.b]\n{BASE}curve_take=true\ncurve_risk_sell=0.04\n\n[lane.c]\n{BASE}exit_module='round_trip'\n")
    va, vb, vc = (lanes.settings_of(got[n], s) for n in "abc")
    assert va == vb and "sell_chain" not in va  # no chain: the version every such lane had
    assert vc["sell_chain"] == repr(("profit_take", "trail", "long_hold")) and {k: v for k, v in vc.items() if k != "sell_chain"} == va


# ---- integration: lane file -> watcher -> sale signals ----
def test_integration_the_chain_and_the_modules_name_reach_the_watched_bet(tmp_path, monkeypatch):
    plain = bet(tmp_path, monkeypatch, "plain")
    t = plain.targets[next(iter(plain.targets))]
    assert t.sell_chain == () and t.sell_module == ""
    own = bet(tmp_path, monkeypatch, "own", extra="sell_chain=['trail','loss']")
    t = own.targets[next(iter(own.targets))]
    assert t.sell_chain == ("trail", "loss") and t.sell_module == ""
    mod = bet(tmp_path, monkeypatch, "mod", extra="exit_module='rt'\n\n[exit.rt]\nsteps=['profit_take','loss']\n[exit.rt.profit]\n[exit.rt.loss]")
    t = mod.targets[next(iter(mod.targets))]
    assert t.sell_chain == ("profit_take", "loss") and t.sell_module == "rt"


def test_integration_the_default_order_written_out_sells_a_loss_exactly_as_no_chain_does(tmp_path, monkeypatch):
    plain = first_sale(bet(tmp_path, monkeypatch, "plain", max_loss=0), 0.20, 450)
    full = first_sale(bet(tmp_path, monkeypatch, "full", max_loss=0, extra=f"sell_chain={FULL}"), 0.20, 450)
    assert plain is not None and full is not None
    assert plain[1].startswith("curve risk exit: the chance the side pays") and "[sell chain" not in plain[1]  # no chain: the reason as it always was
    assert full[0] == plain[0]  # the same second
    assert full[1] == plain[1] + " [sell chain lane: never_ahead > under_even > profit_take > trail > long_hold > loss | sold at loss]"


def test_integration_the_default_order_written_out_takes_a_gain_exactly_as_no_chain_does(tmp_path, monkeypatch):
    out = []
    for name, extra in (("plain", ""), ("full", f"sell_chain={FULL}")):
        w = bet(tmp_path, monkeypatch, name, take="true", extra=extra)
        assert climb(w, 0.82, 0.92, 700) is None
        out.append(level(w, 0.92, 700))
    plain, full = out
    assert plain is not None and full is not None
    assert plain.why.startswith("curve risk exit (profit take)") and "[sell chain" not in plain.why
    assert full.why == plain.why + " [sell chain lane: never_ahead > under_even > profit_take > trail > long_hold > loss | sold at profit_take]"


def test_integration_the_default_order_written_out_trails_exactly_as_no_chain_does(tmp_path, monkeypatch):
    out = []
    for name, extra in (("plain", ""), ("full", f"\nsell_chain={FULL}")):
        w = bet(tmp_path, monkeypatch, name, take="true", extra="curve_take_trail=1.0\ncurve_take_trail_live=true" + extra)
        assert run_up(w) is None and tick(w, 0.89, 250) == []
        out.append(tick(w, 0.86, 249))
    plain, full = out
    assert plain and full and plain[0].why.startswith(TRAIL)
    assert full[0].why == plain[0].why + " [sell chain lane: never_ahead > under_even > profit_take > trail > long_hold > loss | sold at trail]"


def test_integration_a_chain_without_the_loss_step_does_not_sell_a_bet_far_under_the_line(tmp_path, monkeypatch):
    assert sold_after(bet(tmp_path, monkeypatch, "plain", max_loss=0), 0.20, 450) is not None  # the default order sells it
    w = bet(tmp_path, monkeypatch, "no_loss", max_loss=0, extra="sell_chain=['never_ahead','under_even','profit_take','trail','long_hold']")
    assert sold_after(w, 0.20, 450) is None  # the loss step is not in the chain, so it is not asked: the loser rides


def test_integration_a_chain_without_the_loss_step_does_not_sell_at_the_max_loss_either(tmp_path, monkeypatch):
    assert slide_at(bet(tmp_path, monkeypatch, "plain"), 450)  # the sliding bet of 2026-11-13: sold at the max loss
    assert slide_at(bet(tmp_path, monkeypatch, "no_loss", extra="sell_chain=['profit_take','trail','long_hold']"), 450) == []


def test_integration_a_chain_without_profit_take_and_trail_does_not_take_a_gain(tmp_path, monkeypatch):
    plain = bet(tmp_path, monkeypatch, "plain", take="true")
    assert climb(plain, 0.82, 0.92, 700) is None and level(plain, 0.92, 700) is not None  # the default order takes it
    w = bet(tmp_path, monkeypatch, "no_take", take="true", extra="sell_chain=['never_ahead','under_even','long_hold','loss']")
    assert climb(w, 0.82, 0.92, 700) is None and level(w, 0.92, 700) is None  # the same gain, the same lane settings: nothing asks for it


def test_integration_a_chain_without_profit_take_and_trail_still_sells_a_loss(tmp_path, monkeypatch):
    got = first_sale(bet(tmp_path, monkeypatch, "loss_only", max_loss=0, take="true", extra="sell_chain=['long_hold','loss']"), 0.20, 450)
    assert got is not None and got[1].endswith("[sell chain lane: long_hold > loss | sold at loss]")


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


def test_integration_the_note_names_the_exit_module_and_the_step_that_sold(tmp_path, monkeypatch):
    mod = "exit_module='rt'\n\n[exit.rt]\nsteps=['profit_take','long_hold','loss']\n[exit.rt.profit]\n[exit.rt.loss]"
    lost = first_sale(bet(tmp_path, monkeypatch, "lost", max_loss=0, extra=mod), 0.20, 450)
    assert lost is not None and lost[1].endswith(" [sell chain rt: profit_take > long_hold > loss | sold at loss]")
    w = bet(tmp_path, monkeypatch, "won", take="true", extra=mod)
    assert climb(w, 0.82, 0.92, 700) is None
    won = level(w, 0.92, 700)
    assert won is not None and won.why.startswith("curve risk exit (profit take)") and won.why.endswith(" [sell chain rt: profit_take > long_hold > loss | sold at profit_take]")


def test_integration_the_long_hold_is_a_hold_only_for_the_steps_after_it(tmp_path, monkeypatch):
    with_hour_view(monkeypatch, 0.55)  # the long layer says the side is likely to pay: over the lane's 0.4 with seven minutes left
    assert slide_at(bet(tmp_path, monkeypatch, "plain", confirm=0.4), 450) == []  # the default order: the hold comes before the loss sale
    assert slide_at(bet(tmp_path, monkeypatch, "hold_first", confirm=0.4, extra="sell_chain=['long_hold','loss']"), 450) == []
    out = slide_at(bet(tmp_path, monkeypatch, "loss_first", confirm=0.4, extra="sell_chain=['loss','long_hold']"), 450)
    assert out and out[0].why.startswith("curve risk exit (max loss)") and out[0].why.endswith("| sold at loss]")  # asked first, the loss step sells before the hold is reached
    out = slide_at(bet(tmp_path, monkeypatch, "no_hold", confirm=0.4, extra="sell_chain=['loss']"), 450)
    assert out and out[0].why.startswith("curve risk exit (max loss)")  # left out, the hold is not asked


def test_integration_a_chain_lanes_loss_sale_is_still_placed_as_an_order(tmp_path, monkeypatch):
    """The sell executor lets a sale at a loss through by the START of its reason; the note is at the end, so a chain lane's loss sale still becomes an order and closes the bet."""
    from kalshi_moonshot import config
    from kalshi_moonshot.store import Store
    from tests.test_risk_exit_flow import PATH, Client, yes_bid

    c = Client(bid=0.56)
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": True)
    w = held(tmp_path, monkeypatch, live=True, client=c, extra=f"sell_chain={FULL}")
    job = None
    for k, bid in enumerate(PATH):
        yes_bid(w, bid, 450 - 3 * k)
        if not w._jobs.empty():
            job = w._jobs.get_nowait()
            break
    assert job is not None and job.why.startswith("curve risk exit (max loss)") and job.why.endswith("| sold at loss]")
    w._sell(*job)
    assert len(c.sold) == 1 and len(Store(config.load().db_path).open_bets()) == 0
    assert signals(w) == []
