"""The wave lane's dip-and-peak buy on PriskP's curve reading (wavedip.py, buy step `wave_dip`; 2026-11-27, PAPER on the wave_shape lane).

Unit tests of the rule on curve readings; integration through a lane file -> lane refresh -> the buy chain's decision, the per-quote recheck of a resting buy and the runner's evaluation, memory only and inside the tick budget."""
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


def reading(cls="against", swing=1.2, regime="curve", **kw):
    return {"class": cls, "swing_u": swing, "prev_u": 1.5, "gb_u": 0.0, "age": 0.4, "travel": 0.3, "at_extreme": False, "reach": 0.4, "regime": regime, **kw}


def ask(side, t, c, resting=False):
    return wavedip.judge("wt", INDEX, OPEN, side, OPEN + t, c, resting)


# ---- unit: the rule ----
def test_unit_a_dip_that_has_run_a_typical_minute_in_a_curve_market_buys_yes_and_a_peak_buys_no(mem):
    ok, why = ask("yes", 100, reading())
    assert ok and "wave dip (fired): buys YES" in why and "swing 1.20 typical minutes down" in why
    ok, why = ask("no", 100, reading())
    assert ok and "wave peak (fired): buys NO" in why and "typical minutes up" in why


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
    assert ask("yes", 100, reading())[0]
    assert ask("yes", 100.5, reading())[0]  # the same decision asked twice (the review, then the evaluation)
    ok, why = ask("yes", 130, reading(swing=1.9))
    assert not ok and "once per swing" in why
    assert not ask("yes", 140, reading(cls="none", swing=0.0))[0]  # no swing read for a moment: the dip is not over
    assert not ask("yes", 150, reading(swing=2.2))[0]
    assert not ask("yes", 200, reading(cls="inside", swing=0.6))[0]  # the dip turned: the swing is over
    assert ask("yes", 320, reading(swing=1.1))[0]  # the next dip is a new swing
    assert ask("no", 320, reading())[0]  # the sides are counted apart


def test_unit_a_resting_buy_stands_while_the_swing_runs_against_the_side_and_is_out_when_it_turns(mem):
    assert not ask("yes", 90, reading(), resting=True)[0]  # nothing fired: no order stands
    assert ask("yes", 100, reading())[0]
    ok, why = ask("yes", 112, reading(swing=1.6), resting=True)
    assert ok and "its order stands" in why and "12s ago" in why
    ok, why = ask("yes", 118, reading(regime="steady"), resting=True)
    assert not ok and "not a curve market" in why
    ok, why = ask("yes", 125, reading(cls="inside", swing=0.5), resting=True)
    assert not ok and "does not run against" in why


# ---- integration: the lane file, the chain, the recheck, the runner ----
def test_integration_the_lane_file_loads_the_module(tmp_path, monkeypatch):
    sp = spec(tmp_path, monkeypatch)
    assert sp.buy_chain == ("price", "entry_window", "wave_dip", "wave_trend") and sp.buy_module == "wave_dip_buy"
    assert "wave_dip" in lanes.BUY_STEPS and "wave_dip" in buychain.STEPS and "wave_dip" not in buychain.NEEDS_ESTIMATE


def fire_second(side="yes", lo=121, hi=400):
    for t in range(lo, hi):
        c = wavefacts.curve(INDEX, OPEN, OPEN + t + 0.5, K, side, 900.0 - t - 0.5)
        if c and c["regime"] == "curve" and c["class"] == "against" and c["swing_u"] >= wavefacts.TREND_MIN:
            return t
    raise AssertionError("no dip in the plan")


def test_integration_the_chain_buys_yes_in_the_dip_from_memory_only_and_not_before_or_again(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    feed()
    t_fire = fire_second("yes")
    assert 150 <= t_fire <= 185, t_fire  # the dip starts at 120 s and falls 2.5 u in 90 s: a typical minute down is read about 45 s in
    early = OPEN + t_fire - 7.5
    with memguard.decision("buy"):
        bet, note = buychain.decide(buychain.Ctx(s, sp, now=early), shot(early, "yes"))
    assert not bet and "out at wave_dip" in note and "not a real dip yet" in note
    now = OPEN + t_fire + 0.5
    with memguard.decision("buy"):
        bet, note = buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "yes"))
    assert bet, note
    assert "[chain wave_dip_buy: price > entry_window > wave_dip > wave_trend |" in note and "wave dip (fired): buys YES" in note and "typical minutes down" in note
    assert "NO out at price" in note  # the other side is the dear one here: the lane's band keeps the buy to the cheap side
    assert not memguard.breaches
    later = now + 30.0
    bet, note = buychain.decide(buychain.Ctx(s, sp, now=later), shot(later, "yes"))
    assert not bet and "once per swing" in note


def test_integration_the_price_step_keeps_the_lane_to_the_cheap_side(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    feed()
    now = OPEN + fire_second("yes") + 0.5
    for dear in (0.45, 0.12):
        bet, note = buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "yes", ask=dear))
        assert not bet and "YES out at price" in note, note
    assert buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "yes", ask=0.30))[0]  # the refused looks did not use the swing's buy up


def test_integration_a_resting_buy_survives_the_per_quote_recheck_in_the_dip_and_is_withdrawn_when_it_turns(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import restcheck

    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    feed()
    t_fire = fire_second("yes")
    now = OPEN + t_fire + 0.5
    assert buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "yes"))[0]
    later = now + 8.0
    v = restcheck.recheck(s, sp, shot(later, "yes"), 0.29, 0.28, 0.30, later)
    assert v.action != "cancel", v
    turned = OPEN + 260.5  # the dip ended at 210 s and the spot has come back up: the swing runs with the YES now
    v = restcheck.recheck(s, sp, shot(turned, "yes"), 0.29, 0.28, 0.30, turned)
    assert v.action == "cancel" and "wave_dip" in v.why, v


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
    assert "[chain wave_dip_buy: price > entry_window > wave_dip > wave_trend |" in out.note and "wave_dip yes (wave dip (fired)" in out.note, out.note


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
