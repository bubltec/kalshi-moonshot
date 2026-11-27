"""The wave lane's dip-and-peak buy on PriskP's curve reading (wavedip.py, buy step `wave_dip`; 2026-11-27, PAPER on the wave_shape lane).

Unit tests of the rule on curve readings; integration through a lane file -> lane refresh -> the buy chain's decision, the per-quote recheck of a resting buy and the runner's evaluation, memory only and inside the tick budget."""
import dataclasses
import functools
import random
import time

import pytest

from kalshi_moonshot import buychain, config, lanes, memguard, wavedip, wavefacts, waveturn
from kalshi_moonshot import ticks as ticks_mod
from tests.test_wave_turn import INDEX, OPEN, TICKER, K, shot, window_path

TOML = """
[lane.wt]
estimator = "market"
needs_model_edge = false
fixed_stake = 5.0
sides = ["yes", "no"]
price = [0.18, 0.40]
hours = [0.0, 0.26]
entry_elapsed = [0, 15]
series = ["KXBTC15M"]
timed_suffix = "15M"
buy_module = "wave_dip_buy"

[module.wave_dip_buy]
steps = ["price", "entry_window", "wave_dip", "wave_trend"]
"""
# from the target: flat 2 minutes, a dip of 2.5 u, back up 5 u through the target (a peak forming), down 2.5 u, flat
PLAN = [(120, 0.0), (90, -2.5), (90, 2.5), (90, 2.5), (90, -2.5), (300, 0.0)]


@pytest.fixture
def mem(monkeypatch):
    def clear():
        ticks_mod.history.clear()
        ticks_mod.mids.clear()
        ticks_mod.strikes._v.clear()
        waveturn.clear()
        wavedip.clear()
        wavefacts._before.clear()
        wavefacts._big.clear()
        buychain._THROTTLED.clear()

    clear()
    monkeypatch.setattr(buychain, "THROTTLE_S", 0.0)
    yield
    clear()


def pre_path(seed=3, n=3700):
    """A little over an hour before the open, a random walk ending at the target (wavefacts reads ten seconds more than the hour)."""
    rnd = random.Random(seed)
    xs, x = [], 0.0
    for _ in range(n):
        x += rnd.gauss(0.0, 1.0)
        xs.append(x)
    return [K + v - xs[-1] for v in xs]


def feed(plan=PLAN):
    pre = pre_path()
    u = waveturn.hour_of(pre[-waveturn.PRE:]).u
    window = window_path(plan, u)
    rows = [(OPEN - len(pre) + k, v) for k, v in enumerate(pre)] + [(OPEN + k, v) for k, v in enumerate(window)]
    ticks_mod.history.warm(INDEX, rows, rows[0][0] - 1.0)
    ticks_mod.strikes.note(TICKER, K)


def spec(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(TOML)
    return lanes.lane_specs(config.load())["wt"]


def reading(cls="against", swing=1.2, regime="curve", gb=0.25, start=120, **kw):
    return {"class": cls, "swing_u": swing, "prev_u": 1.5, "gb_u": gb, "age": 0.4, "travel": 0.3, "at_extreme": False, "reach": 0.4, "regime": regime, "swing_start": OPEN + start, **kw}


def ask(side, t, c, resting=False, last_exit=None):
    return wavedip.judge("wt", INDEX, OPEN, side, OPEN + t, c, resting, last_exit)


# ---- unit: the rule ----
def test_unit_a_dip_that_starts_to_turn_in_a_curve_market_buys_yes_and_a_cresting_peak_buys_no(mem):
    ok, why = ask("yes", 200, reading())
    assert ok and "wave dip (fired): buys YES as the dip starts to turn" in why and "swing 1.20 typical minutes down" in why and "gave back 0.25" in why
    ok, why = ask("no", 200, reading())
    assert ok and "wave peak (fired): buys NO as the peak starts to turn" in why and "typical minutes up" in why


def test_unit_a_dip_still_falling_is_not_bought(mem):
    """The first version bought here (the swing has run a typical minute, nothing about its turning): the lane's give-up sold 66 such live buys at the low a median 78 s later."""
    for gb in (0.0, 0.1, 0.19):
        ok, why = ask("yes", 200, reading(gb=gb, swing=2.0))
        assert not ok and "has not started to turn" in why, gb
    assert ask("yes", 205, reading(gb=0.2, swing=2.0))[0]


def test_unit_no_buy_outside_a_curve_market_on_a_swing_with_the_side_on_a_young_swing_or_an_unread_curve(mem):
    assert ask("yes", 100, None) == (False, "the curve cannot be read yet (the hour before the open or the path since it is not in memory)")
    ok, why = ask("yes", 100, reading(regime="steady"))
    assert not ok and "not a curve market" in why
    for cls in ("inside", "turned", "none"):
        ok, why = ask("yes", 100, reading(cls=cls))
        assert not ok and "does not run against the YES" in why, cls
    ok, why = ask("yes", 100, reading(swing=0.8))
    assert not ok and "under 1" in why and "not a real dip yet" in why


def test_unit_once_per_swing_and_a_new_swing_fires_again(mem):
    assert ask("yes", 200, reading())[0]
    assert ask("yes", 200.5, reading())[0]  # the same decision asked twice (the review, then the evaluation)
    ok, why = ask("yes", 230, reading(swing=1.9))
    assert not ok and "once per swing" in why
    assert not ask("yes", 240, reading(cls="none", swing=0.0))[0]
    assert not ask("yes", 250, reading(cls="inside", swing=0.6, start=235))[0]  # the dip turned
    assert not ask("yes", 300, reading(swing=2.2))[0]  # the reading is back on the same dip (the same start): still its one buy
    assert ask("yes", 420, reading(swing=1.1, start=380))[0]  # the next dip is a new swing
    assert ask("no", 420, reading())[0]  # the sides are counted apart


def test_unit_the_move_that_closed_a_bet_is_not_a_new_entry(mem):
    """The reversal (2026-11-27, COPPER 04:28 and XRP 04:51 bought the other side 5 s after a sale, ZEC 05:45 14 s after): the rise that made the lane sell its YES is, for the step, a peak for the NO. A swing already running at the sale is refused; a swing that begins after it is a new one."""
    sold = OPEN + 300.0
    ok, why = ask("no", 305, reading(start=250), last_exit=sold)  # the rise began at 250 s, the lane sold at 300 s
    assert not ok and "already running when the lane sold its bet on this market 5s ago" in why and "a new peak must begin after the sale" in why
    ok, why = ask("yes", 330, reading(start=200), last_exit=sold)  # the same side again in the dip that failed
    assert not ok and "already running" in why
    assert ask("no", 420, reading(start=340), last_exit=sold)[0]  # a peak that began after the sale
    wavedip.clear()
    assert ask("no", 305, reading(start=250), last_exit=None)[0]  # no sale on this market: nothing to guard


def test_unit_a_resting_buy_stands_while_the_turn_holds_and_is_out_when_it_fails_or_the_price_has_left(mem):
    assert not ask("yes", 190, reading(), resting=True)[0]  # nothing fired: no order stands
    assert ask("yes", 200, reading())[0]
    ok, why = ask("yes", 212, reading(swing=1.6, gb=0.3), resting=True)
    assert ok and "its order stands: the dip is still turning" in why and "12s ago" in why
    ok, why = ask("yes", 215, reading(cls="inside", swing=0.6, start=205), resting=True)  # the reading has called the new swing: it is young, the price has not left
    assert ok and "has turned and the new swing is 0.60" in why
    assert ask("yes", 216, reading(cls="none", swing=0.0), resting=True)[0]
    ok, why = ask("yes", 240, reading(cls="inside", swing=1.3, start=205), resting=True)
    assert not ok and "is over" in why
    wavedip.clear()
    assert ask("yes", 200, reading())[0]
    ok, why = ask("yes", 210, reading(swing=1.9, gb=0.05), resting=True)  # the spot went back to the low: the turn did not hold
    assert not ok and "the turn did not hold" in why
    wavedip.clear()
    assert ask("yes", 200, reading())[0]
    ok, why = ask("yes", 208, reading(regime="steady"), resting=True)
    assert not ok and "not a curve market" in why


# ---- integration: the lane file, the chain, the recheck, the runner ----
def test_integration_the_lane_file_loads_the_module(tmp_path, monkeypatch):
    sp = spec(tmp_path, monkeypatch)
    assert sp.buy_chain == ("price", "entry_window", "wave_dip", "wave_trend") and sp.buy_module == "wave_dip_buy"
    assert "wave_dip" in lanes.BUY_STEPS and "wave_dip" in buychain.STEPS and "wave_dip" not in buychain.NEEDS_ESTIMATE


def fire_second(side="yes", lo=121, hi=400):
    for t in range(lo, hi):
        c = wavefacts.curve(INDEX, OPEN, OPEN + t + 0.5, K, side, 900.0 - t - 0.5)
        if c and c["regime"] == "curve" and c["class"] == "against" and c["swing_u"] >= wavefacts.TREND_MIN and c["gb_u"] >= wavedip.START_GB:
            return t
    raise AssertionError("no turning dip in the plan")


def test_integration_the_chain_buys_yes_as_the_dip_starts_to_turn_from_memory_only_and_not_before_or_again(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    feed()
    t_fire = fire_second("yes")
    assert 205 <= t_fire <= 250, t_fire  # the dip runs from 120 s to 210 s; its turn is read a little after the low (the reading is on a 30 s median)
    falling = OPEN + 180.5  # the dip has run more than a typical minute and is still falling
    with memguard.decision("buy"):
        bet, note = buychain.decide(buychain.Ctx(s, sp, now=falling), shot(falling, "yes"))
    assert not bet and "out at wave_dip" in note and "has not started to turn" in note
    now = OPEN + t_fire + 0.5
    with memguard.decision("buy"):
        bet, note = buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "yes"))
    assert bet, note
    assert "[chain wave_dip_buy: price > entry_window > wave_dip > wave_trend |" in note and "wave dip (fired): buys YES as the dip starts to turn" in note and "typical minutes down" in note
    assert "NO out at price" in note  # the other side is the dear one here: the lane's band keeps the buy to the cheap side
    assert not memguard.breaches
    later = now + 3.0
    bet, note = buychain.decide(buychain.Ctx(s, sp, now=later), shot(later, "yes"))
    assert not bet and "out at wave_dip" in note


def test_integration_the_price_step_keeps_the_lane_to_the_cheap_side(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    feed()
    now = OPEN + fire_second("yes") + 0.5
    for dear in (0.45, 0.12):
        bet, note = buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "yes", ask=dear))
        assert not bet and "YES out at price" in note, note
    assert buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "yes", ask=0.30))[0]  # the refused looks did not use the swing's buy up


def test_integration_a_resting_buy_survives_the_per_quote_recheck_while_the_turn_holds_and_is_withdrawn_when_the_price_has_left(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import restcheck

    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    feed()
    t_fire = fire_second("yes")
    now = OPEN + t_fire + 0.5
    assert buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "yes"))[0]
    later = now + 4.0
    v = restcheck.recheck(s, sp, shot(later, "yes"), 0.29, 0.28, 0.30, later)
    assert v.action != "cancel", v
    left = OPEN + 295.5  # the spot is back up through the target: the new swing has run well over a typical minute
    v = restcheck.recheck(s, sp, shot(left, "yes"), 0.29, 0.28, 0.30, left)
    assert v.action == "cancel" and "wave_dip" in v.why, v


def test_integration_the_step_reads_the_lanes_last_sale_from_the_books_at_a_decision(tmp_path, monkeypatch, mem):
    class Books:
        def __init__(self, sold):
            self.sold, self.asked = sold, []

        def last_exit_ts(self, ticker, lane):
            self.asked.append((ticker, lane))
            return self.sold

    spec(tmp_path, monkeypatch)
    feed()
    t_fire = fire_second("yes")
    now = OPEN + t_fire + 0.5
    books = Books(now - 5.0)  # the lane sold a bet on this market 5 s ago, while this dip was already running
    ok, why = wavedip.admits(shot(now, "yes"), now, K, False, books)
    assert not ok and "already running when the lane sold its bet" in why and books.asked == [(TICKER, "wt")]
    ok, why = wavedip.admits(shot(now, "yes"), now, K, False, Books(None))
    assert ok and "fired" in why


def test_integration_a_resting_buy_never_rests_under_the_lanes_floor(tmp_path, monkeypatch, mem):
    """12 of the lane's 145 live buys of 2026-11-27 filled at 0.14 to 0.17 on a range that starts at 0.18: the order rests under the ask, and with the ask at 0.18 to 0.19 that was under the floor."""
    from kalshi_moonshot import restcheck, restprice

    sp = spec(tmp_path, monkeypatch)
    sp = dataclasses.replace(sp, buy_rest_seconds=5.0, buy_rest_below=0.02, buy_rest_follow=True)
    assert restprice.rest_price(sp, "yes", 0.19, [], OPEN) == pytest.approx(0.18)  # 0.17 would be under the floor
    assert restprice.rest_price(sp, "yes", 0.30, [], OPEN) == pytest.approx(0.28)
    assert restcheck.want_price(sp, "yes", 0.19, [], OPEN) == pytest.approx(0.18)
    assert restcheck.want_price(sp, "yes", 0.25, [], OPEN) == pytest.approx(0.23)
    price, why = runner_price(sp, 0.19)
    assert price == pytest.approx(0.18) and why == ""
    price, why = runner_price(sp, 0.18)  # nothing fits between the floor and the ask
    assert price is None and "floor" in why
    s = config.load()
    feed()
    now = OPEN + fire_second("yes") + 0.5
    assert buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "yes", ask=0.19))[0]
    v = restcheck.recheck(s, sp, shot(now + 2.0, "yes", ask=0.19), 0.18, 0.17, 0.18, now + 2.0)  # the ask fell to the floor: no price is left for the order
    assert v.action == "cancel" and "floor" in v.why, v


def runner_price(sp, ask):
    from kalshi_moonshot import runner

    return runner._resting_price(shot(OPEN + 300.5, "yes", ask=ask), sp, None)


def test_integration_through_the_runners_evaluation(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import regime, runner
    from kalshi_moonshot.store import Store
    from tests.decide_support import Fixed

    spec(tmp_path, monkeypatch)
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    feed()
    now = OPEN + fire_second("yes") + 0.5
    monkeypatch.setattr(buychain, "Ctx", functools.partial(buychain.Ctx, now=now))
    out = runner.evaluate(s, None, None, store, shot(now, "yes"), estimators={"market": Fixed()})
    assert "[chain wave_dip_buy: price > entry_window > wave_dip > wave_trend |" in out.note and "wave_dip yes (wave dip (fired)" in out.note and "starts to turn" in out.note, out.note


def test_integration_one_tick_is_fast_and_memory_only(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    feed()
    for side in ("yes", "no"):  # the first look builds the market's hour profile (once per market, kept)
        buychain.STEPS["wave_dip"](buychain.Ctx(s, sp, now=OPEN + 300.5), shot(OPEN + 300.5, side))
    began = time.perf_counter()
    with memguard.decision("buy"):
        for side in ("yes", "no"):
            buychain.STEPS["wave_dip"](buychain.Ctx(s, sp, now=OPEN + 600.5), shot(OPEN + 600.5, side))
    assert time.perf_counter() - began < 0.06 and not memguard.breaches
