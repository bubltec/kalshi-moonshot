"""The PriskP buy step through memory (priskp.admit and buychain's `priskp` step): the checks, the confirmation, the user's window and dead ask band, the shift block against the current target, and what an unread check does."""
import math
import random
from datetime import UTC, datetime

import pytest

from kalshi_moonshot import buychain, config, priskp, ticks
from kalshi_moonshot.strategy import LongShot

TICKER = "KXBTC15M-26NOV200115-15"
INDEX = "BRTI"
OPEN = 2_000_000.0  # the market's open; it closes at OPEN + 900
EVERYTHING = [-1e6, -1e5, -1e4, -1e3, -1e2, 1e6]  # cutoffs that put every reading in fifth 5 (no anomaly)


@pytest.fixture(autouse=True)
def clean():
    priskp.clear(); priskp._read_cache.clear()
    ticks.history.clear(); ticks.mids.clear(); ticks.strikes.clear()
    yield
    priskp.clear(); priskp._read_cache.clear()
    ticks.history.clear(); ticks.mids.clear(); ticks.strikes.clear()


@pytest.fixture(autouse=True)
def no_price_peak(monkeypatch):
    """The fed quotes are a random walk whose price may happen to be climbing: the price-peak wait (priskp._price_rising) is off here unless a test turns it on, so the other waits are tested as they were."""
    monkeypatch.setattr(priskp, "_price_rising", lambda *a, **k: None)


def feed(levels, upto, strike=100.0, seed=1):
    """An hour and more of 1-second ticks: `levels` is a function of seconds since OPEN returning the spot, noise added; quotes on a random walk."""
    rnd = random.Random(seed)
    t = OPEN - 4000.0
    while t <= upto:
        ticks.history.note(INDEX, t, levels(t - OPEN) * math.exp(rnd.gauss(0, 0.0002)))
        t += 1.0
    m, t = 0.55, OPEN - 60.0
    while t <= upto:
        m = min(0.95, max(0.05, m + rnd.gauss(0, 0.004)))
        ticks.mids.note(TICKER, t, m - 0.01, m + 0.01)
        t += 1.0
    ticks.strikes.note(TICKER, strike)


def shot(side="yes", ask=0.70):
    return LongShot(ticker=TICKER, title="t", rules="r", bid=ask - 0.01, ask=ask, volume=1.0, close_time=datetime.fromtimestamp(OPEN + 900.0, UTC).isoformat(), hours_to_close=0.25, series="KXBTC15M", side=side)


def cuts():
    priskp.load({f"KXBTC15M|{b}|{c}": EVERYTHING for b in range(5) for c in priskp.CHECKS})


def ask_at(minute, side="yes", ask=0.70, s=None):
    now = OPEN + minute * 60.0
    return priskp.admit(s, shot(side, ask), now, (OPEN + 900.0 - now) / 3600.0)


def test_without_a_cutoffs_file_it_waits_it_does_not_buy():
    feed(lambda s: 100.5, OPEN + 600.0)
    ok, why = ask_at(5.0)
    assert not ok and "no cutoffs file" in why


def test_it_buys_on_the_first_candidate_reading_inside_the_window():
    feed(lambda s: 100.5, OPEN + 600.0)
    cuts()
    got = [ask_at(5.0 + k / 60.0) for k in range(3)]  # readings 1 s apart
    assert [g[0] for g in got] == [True, True, True]  # one reading (2026-11-25, user: "it should be like 5s max")
    assert "verb: buy" in got[0][1] and "why: pass" in got[0][1] and "need: 1" in got[0][1]
    assert "failing: []" in got[0][1] and "cushion_fifth: 5" in got[0][1]


def test_the_confirmation_is_one_reading_whatever_the_time_left():
    from kalshi_moonshot import priskp

    assert priskp.need_readings(780.0) == 1 and priskp.need_readings(300.0) == 1 and priskp.need_readings(0.0) == 1
    assert priskp.EVERY <= 5.0  # the readings are cut at most every 5 s and read from memory in between; the market's pricing is live on every look (user: "the only thing we need evaluated every tick, is the market pricing")


def test_outside_minutes_2_to_14_and_in_the_dead_ask_band_it_does_not_buy():
    feed(lambda s: 100.5, OPEN + 880.0)
    cuts()
    for k in range(4):
        assert ask_at(1.0 + k / 6.0)[0] is False  # before the window
    ok, why = ask_at(14.6)
    assert ok is False and "why: window" in why
    for k in range(4):
        ok, why = ask_at(6.0 + k / 6.0, ask=0.50)
    assert ok is False and "dead_ask_band" in why
    ok, why = ask_at(7.0, ask=0.45)
    assert ok is False and "dead_ask_band" in why


def test_a_shift_down_out_of_the_cushion_blocks_yes_and_not_no_against_the_current_target():
    feed(lambda s: 100.6 if s < 120 else 99.2, OPEN + 600.0)  # above the 100 strike, then a step down under it that holds for 8 minutes
    cuts()
    outs = {side: [ask_at(7.0 + k / 6.0, side) for k in range(3)] for side in ("yes", "no")}
    assert outs["yes"][2][0] is False and "shift_out_of_cushion" in outs["yes"][2][1]
    assert outs["no"][2][0] is True


def test_the_same_step_against_another_target_blocks_nothing():
    feed(lambda s: 100.6 if s < 120 else 99.2, OPEN + 600.0, strike=95.0)  # a target under both levels: the step never left the YES cushion
    cuts()
    outs = [ask_at(7.0 + k / 6.0, "yes") for k in range(3)]
    assert outs[2][0] is True


def test_an_unread_check_is_no_buy():
    feed(lambda s: 100.5, OPEN + 600.0)
    priskp.load({f"KXBTC15M|{b}|{c}": EVERYTHING for b in range(5) for c in priskp.CHECKS if c != "history"})  # a check with no history for its bucket
    got = [ask_at(5.0 + k / 6.0) for k in range(4)]
    assert not any(g[0] for g in got) and "history" in got[3][1] and "why: unread" in got[3][1]


def test_the_lane_step_asks_priskp_through_the_buy_chain():
    feed(lambda s: 100.5, OPEN + 600.0)
    cuts()
    s = config.load()
    spec = None  # the step reads no lane setting
    assert "priskp" in buychain.STEPS and "priskp" in __import__("kalshi_moonshot.lanes", fromlist=["BUY_STEPS"]).BUY_STEPS
    now = OPEN + 5.0 * 60.0
    res = [buychain.STEPS["priskp"](buychain.Ctx(s=s, spec=spec, now=now + k * 10.0), shot()) for k in range(3)]
    assert [r.ok for r in res] == [True, True, True] and res[0].why.startswith("verb: buy")


def test_the_buy_reads_the_facts_board_and_the_veto_rule_is_the_board_s_not_the_step_s():
    from kalshi_moonshot import facts

    feed(lambda s: 100.6 if s < 120 else 99.2, OPEN + 600.0)
    cuts()
    outs = [ask_at(7.0 + k / 6.0, "yes") for k in range(3)]
    ok, why = outs[2]
    assert ok is False and "shift_out_of_cushion" in why and "[facts read: priskp_screen=" in why and "shift_out=" in why  # the note shows what the buy was judged on
    assert "priskp" in facts.VETOED_STEPS and any(rule[0] == "shift_out" and rule[2] == ("priskp",) for rule in facts.VETO_RULES)
    assert facts.NEEDS["priskp"] == ("priskp_screen", "shift_out", "early_swings", "hour_view", "price_gap", "waves_before", "wave_now", "wave_leg") and all(n in facts.READERS for n in facts.NEEDS["priskp"])  # the wave facts (2026-11-25) are on the board for every step to read


def test_a_buy_logs_the_hour_view_and_whether_the_risk_was_sure():
    """2026-11-23: the hour view (hourview.py, built in the background) is read from memory and written on the buy's line; "unsure" when a check is in fifth 4 and not 5. Logged only: it never changes the verdict."""
    from kalshi_moonshot import hourview

    feed(lambda s: 100.6, OPEN + 700.0)
    cuts()
    hourview.clear()
    outs = [ask_at(5.0 + k / 6.0, "yes") for k in range(3)]
    assert outs[2][0] is True and "hour_view" in outs[2][1] and "hour_drift" not in outs[2][1]  # no view built yet: nothing to log, the buy is as before
    now = OPEN + 5.0 * 60.0 + 20.0
    hourview._VIEW[TICKER] = {"u": 0.0004, "drift_yes": 3.0, "range": 5.0, "cross": 0.0, "eff": 0.2, "hi_yes_ago": 1.0, "lo_yes_ago": 50.0, "built_at": now}
    ok, why = ask_at(5.0 + 20.0 / 60.0, "yes")
    assert ok is True and "hour_drift: 3.0" in why and "hour_tie: sure" in why  # every check in fifth 5 here: the risk was sure
    hourview.clear()


def test_the_price_gap_is_a_weight_on_the_board_and_never_a_reason_to_refuse():
    """2026-11-26, user: "it should have an influence but it shouldn't be the factor in whether we buy". The gap (chance minus the ask and its fee) is a signed weight on the board's ledger; the buy verdict is the same with a gap of either sign."""
    from kalshi_moonshot import facts

    class T:
        ask = 0.80

    b = facts.Board(None, T(), 0.0, 600.0, 0.78)
    b._v["priskp_screen"] = ({"verb": "buy", "chance": 0.70, "ask": 0.80}, "")
    got = b.view("priskp").get("price_gap")
    assert got["gap"] < 0 and abs(got["gap"] - (0.70 - 0.80 - 0.07 * 0.8 * 0.2)) < 1e-3
    b.view("priskp").weigh(got["gap"], "x")
    assert b.score() == got["gap"] and b.vetoes("priskp") is None  # a ledger entry, no veto
    b2 = facts.Board(None, T(), 0.0, 600.0, 0.78)
    b2._v["priskp_screen"] = ({"verb": "buy"}, "")
    assert b2.view("priskp").get("price_gap") is None  # no chance read: no weight


def test_the_price_gap_uses_the_swing_aware_chance_when_it_is_read():
    """2026-11-26, user: "if you're betting on a curve early you need to be calculating for additional swings and variance possibilities based on the recent history". The gap is built on wave_now's mean-reverting chance when read, else the random-walk one."""
    from kalshi_moonshot import facts

    class T:
        ask = 0.75

    b = facts.Board(None, T(), 0.0, 780.0, 0.72)
    b._v["priskp_screen"] = ({"verb": "buy", "chance": 0.60, "ask": 0.75}, "")
    b._v["wave_now"] = ({"chance": 0.48}, "")  # a spike away from the market's centre: expected to swing back
    got = b.view("priskp").get("price_gap")
    assert got["chance"] == 0.48 and got["swing"] == 0.48 and got["gauss"] == 0.60 and got["gap"] < -0.27
    b2 = facts.Board(None, T(), 0.0, 780.0, 0.72)
    b2._v["priskp_screen"] = ({"verb": "buy", "chance": 0.60, "ask": 0.75}, "")
    b2._v["wave_now"] = (None, "")  # no swing read: the random walk's chance
    got2 = b2.view("priskp").get("price_gap")
    assert got2["chance"] == 0.60 and got2["swing"] is None


def test_in_a_curve_the_trend_entry_buys_without_the_five_checks_passing(monkeypatch):
    """2026-11-26: a curve 'buy' was held at 'confirming' 0 of 1 for ever because the readings in a row counted the five checks (4027 looks on 27 markets in 3 h, one bet). In a curve the count follows the curve's verdict; outside one, the checks."""
    feed(lambda s: 100.5, OPEN + 600.0)
    priskp.load({f"KXBTC15M|{b}|{c}": [1e6, 2e6, 3e6, 4e6, 5e6, 6e6] for b in range(5) for c in priskp.CHECKS})  # every check in fifth 1: the five checks refuse
    curve = {"regime": "curve", "verdict": "buy", "class": "inside", "swing_u": 1.5, "prev_u": 1.2, "gb_u": 0.0, "age": 0.3, "travel": 0.3, "reach": 0.4}
    monkeypatch.setattr(priskp, "_curve_verdict", lambda *a, **k: dict(curve))
    ok, why = ask_at(5.0)
    assert ok and "why: curve_trend_entry" in why and "confirm: 1" in why
    assert not ask_at(5.1, ask=0.50)[0]  # the dead ask band still holds
    priskp._runs.clear()
    monkeypatch.setattr(priskp, "_curve_verdict", lambda *a, **k: dict(curve, verdict="wait"))
    ok, why = ask_at(5.0)
    assert not ok and "why: curve_wait" in why and "confirm: 0" in why
    priskp._runs.clear()
    monkeypatch.setattr(priskp, "_curve_verdict", lambda *a, **k: dict(curve, regime="steady"))
    ok, why = ask_at(5.0)
    assert not ok and "check_not_passing" in why  # a steady market: the five checks decide as before


def test_in_a_curve_the_trend_entry_waits_until_the_move_has_held(monkeypatch):
    """2026-11-26 (user, on SOL 11:15 NO and ZEC 11:30 YES: "They should have bought yes by waiting"): a trend entry waits while the lane's own chance for the bet's side is under 0.5 (the cushion has not come over to the bet's side), and buys once it has. An unread chance waits nothing."""
    feed(lambda s: 100.5, OPEN + 600.0)  # the spot is OVER the 100 target: YES has the cushion, NO does not
    cuts()
    curve = {"regime": "curve", "verdict": "buy", "class": "inside", "swing_u": 1.6, "prev_u": 1.5, "gb_u": 0.0, "age": 0.6, "travel": 0.7, "reach": 0.2}
    monkeypatch.setattr(priskp, "_curve_verdict", lambda *a, **k: dict(curve))
    ok, why = ask_at(5.0, side="no")
    assert not ok and "why: curve_not_held" in why and "confirm: 0" in why
    chance = float(why.split("chance: ")[1].split(";")[0])
    assert chance < priskp.HELD
    ok, why = ask_at(5.0, side="yes")
    assert ok and "why: curve_trend_entry" in why and float(why.split("chance: ")[1].split(";")[0]) >= priskp.HELD
    priskp._runs.clear()
    monkeypatch.setattr(priskp, "_curve_verdict", lambda *a, **k: dict(curve, regime="steady"))
    assert "curve_not_held" not in ask_at(5.0, side="no")[1]  # only in a curve
    assert priskp.HELD == 0.5


def test_in_a_curve_the_trend_entry_waits_at_the_peak_of_its_own_swing_and_buys_after_the_pullback(monkeypatch):
    """2026-11-27 (user, on ETH 00:45 YES bought at the top of a bounce: "It also bought on a peak which it should never do for a favorite"; "I think we should set the off the peak after a pullback on. It will change when we bet which is better"): in a curve a buy whose own swing is at its peak waits (curve_peak), and is bought once the swing has pulled back; outside a curve the reading is only noted."""
    feed(lambda s: 100.5, OPEN + 600.0)
    cuts()
    curve = {"regime": "curve", "verdict": "buy", "class": "inside", "swing_u": 1.6, "prev_u": 1.5, "gb_u": 0.0, "age": 0.6, "travel": 0.7, "reach": 0.2}
    monkeypatch.setattr(priskp, "_curve_verdict", lambda *a, **k: dict(curve))
    at_peak = {"signs": "- + + +", "with": 3, "biggest_u": 6.0, "levels": [-1, 1, 1, 1], "peak": [True, True], "at_peak": True}
    monkeypatch.setattr(priskp, "_nest", lambda *a, **k: dict(at_peak))
    ok, why = ask_at(5.0, side="yes")
    assert not ok and "why: curve_peak" in why and "confirm: 0" in why and "at a peak" in why
    priskp._runs.clear()
    monkeypatch.setattr(priskp, "_nest", lambda *a, **k: dict(at_peak, peak=[False, True], at_peak=False))  # the quarter-size swing has pulled back
    ok, why = ask_at(5.0, side="yes")
    assert ok and "why: curve_trend_entry" in why and "off the peak" in why
    priskp._runs.clear()
    monkeypatch.setattr(priskp, "_nest", lambda *a, **k: None)  # no reading: the wait holds nothing back
    assert ask_at(5.0, side="yes")[0]
    priskp._runs.clear()
    monkeypatch.setattr(priskp, "_nest", lambda *a, **k: dict(at_peak))
    monkeypatch.setattr(priskp, "_curve_verdict", lambda *a, **k: dict(curve, regime="steady"))
    assert "curve_peak" not in ask_at(5.0, side="yes")[1]  # only in a curve


def test_a_curve_entry_waits_on_the_swing_aware_chance_with_a_bar_that_slides_with_the_time_left(monkeypatch):
    """2026-11-26 (user: "Fix it", then "Make sure the early rule has a slide with time left"; 2026-11-25: "If you're betting on a curve early you need to be calculating for additional swings and variance possibilities based on the recent history (hour)"): a curve trend entry waits while wave_now's chance is under 0.5 x the share of the market left. No cut-off minute. A board veto that waits, not a refusal."""
    from kalshi_moonshot import facts

    assert facts.swing_bar(900.0) == 0.5 and abs(facts.swing_bar(600.0) - 1 / 3) < 1e-9 and facts.swing_bar(450.0) == 0.25 and facts.swing_bar(0.0) == 0.0 and facts.swing_bar(2000.0) == 0.5
    feed(lambda s: 100.5, OPEN + 800.0)
    cuts()
    curve = {"regime": "curve", "verdict": "buy", "class": "inside", "swing_u": 1.6, "prev_u": 1.5, "gb_u": 0.0, "age": 0.6, "travel": 0.7, "reach": 0.6}
    monkeypatch.setattr(priskp, "_curve_verdict", lambda *a, **k: dict(curve))
    swing = [0.30]
    monkeypatch.setitem(facts.READERS, "wave_now", lambda b: ({"chance": swing[0]} if swing[0] is not None else None, "test"))
    ok, why = ask_at(3.0)  # 720 s left: the bar is 0.40
    assert not ok and "verb: wait" in why and "why: early_swings_not_cleared" in why and "early_swings=yes" in why
    ok, why = ask_at(6.5)  # 510 s left: the bar has slid to 0.28 and the same reading buys
    assert ok and "why: curve_trend_entry" in why and "early_swings=no" in why
    priskp._runs.clear()
    swing[0] = 0.155  # ZEC 13:30 NO: minute 5.9, the first form's minute-5 cut-off let it through
    ok, why = ask_at(5.9)
    assert not ok and "early_swings_not_cleared" in why
    ok, why = ask_at(11.0)  # 240 s left: the bar is 0.13
    assert ok
    priskp._runs.clear()
    swing[0] = None
    assert ask_at(3.0)[0]  # an unread chance waits nothing
    assert ("early_swings", "early_swings_not_cleared") in [(f, lab) for f, _t, _s, lab in facts.VETO_RULES]


def test_in_a_curve_the_trend_entry_waits_while_its_own_price_is_still_climbing(monkeypatch):
    """2026-11-27 (user, on the XRP 06:30 YES re-buy at the price the lane had just sold at: "The peak reading looks at the spot's swings ... this is a bug we need to fix"; "The wait should just happen"): the peak is the side's OWN PRICE as well as the spot's swings; a buy while its price still climbs at both small sizes waits (curve_peak, peak_by price) and is bought once the price has pulled back."""
    feed(lambda s: 100.5, OPEN + 600.0)
    cuts()
    curve = {"regime": "curve", "verdict": "buy", "class": "inside", "swing_u": 1.6, "prev_u": 1.5, "gb_u": 0.0, "age": 0.6, "travel": 0.7, "reach": 0.2}
    monkeypatch.setattr(priskp, "_curve_verdict", lambda *a, **k: dict(curve))
    monkeypatch.setattr(priskp, "_nest", lambda *a, **k: {"signs": "- + + +", "with": 3, "biggest_u": 6.0, "levels": [-1, 1, 1, 1], "peak": [False, False], "at_peak": False})  # the spot reads off the peak, as it did on the re-buy
    monkeypatch.setattr(priskp, "_price_rising", lambda *a, **k: True)
    ok, why = ask_at(5.0, side="yes")
    assert not ok and "why: curve_peak" in why and "peak_by: price" in why
    priskp._runs.clear()
    monkeypatch.setattr(priskp, "_price_rising", lambda *a, **k: False)  # pulled back
    ok, why = ask_at(5.0, side="yes")
    assert ok and "why: curve_trend_entry" in why
    priskp._runs.clear()
    monkeypatch.setattr(priskp, "_price_rising", lambda *a, **k: None)  # unread
    assert ask_at(5.0, side="yes")[0]
    priskp._runs.clear()
    monkeypatch.setattr(priskp, "_price_rising", lambda *a, **k: True)
    monkeypatch.setattr(priskp, "_curve_verdict", lambda *a, **k: dict(curve, regime="steady"))
    assert "curve_peak" not in ask_at(5.0, side="yes")[1]  # only in a curve


def test_in_a_curve_the_trend_entry_waits_while_the_cushion_is_under_half_the_turn_it_came_from(monkeypatch):
    """2026-11-27 (user: "betting Yes at the end peak of a U shaped curve (with almost no cushion) ... I have never asked for that logic"; "Just seems like a bad idea with low cushion to buy in a curve like that. Would have been better to wait"): the spot's cushion over the target must be at least half the turn the bet's swing started from, both in the market's typical minutes; below it the lane waits for the next place to bet."""
    feed(lambda s: 100.5, OPEN + 600.0)
    cuts()
    curve = {"regime": "curve", "verdict": "buy", "class": "inside", "swing_u": 1.6, "prev_u": 2.0, "gb_u": 0.0, "age": 0.6, "travel": 0.7, "reach": 0.2, "cushion_u": 0.7}
    monkeypatch.setattr(priskp, "_curve_verdict", lambda *a, **k: dict(curve))
    ok, why = ask_at(5.0, side="yes")
    assert not ok and "why: curve_low_cushion" in why and "cushion_u: 0.7" in why and "turn_u: 2.0" in why  # 0.7 against half of 2.0
    priskp._runs.clear()
    monkeypatch.setattr(priskp, "_curve_verdict", lambda *a, **k: dict(curve, cushion_u=1.0))
    assert ask_at(5.0, side="yes")[0]  # exactly half the turn: bought
    priskp._runs.clear()
    monkeypatch.setattr(priskp, "_curve_verdict", lambda *a, **k: {k2: v for k2, v in curve.items() if k2 != "cushion_u"})
    assert ask_at(5.0, side="yes")[0]  # no reading of the cushion: nothing is held back
    priskp._runs.clear()
    monkeypatch.setattr(priskp, "_curve_verdict", lambda *a, **k: dict(curve, regime="steady"))
    assert "curve_low_cushion" not in ask_at(5.0, side="yes")[1]  # only in a curve


def test_the_lane_does_not_buy_the_same_swing_twice_and_waits_for_the_next_one(monkeypatch):
    """2026-11-27 (user, on the re-buys: "why doesn't the rebuy follow priskp logic, which should have done none of this and waited for the next curve"): a bet in the swing the lane already bought waits; once the swing has turned (a new swing start) it is bought again."""
    from types import SimpleNamespace

    from kalshi_moonshot import binaryrisk, wavefacts

    ticks.strikes.note(TICKER, 100.0)
    monkeypatch.setattr(binaryrisk, "curve_index", lambda series: "IDX")
    cur = {"regime": "curve", "swing_start": 1000.0}
    monkeypatch.setattr(wavefacts, "curve", lambda *a, **k: dict(cur))
    monkeypatch.setattr(wavefacts, "leg", lambda index, opened, t, *a, **k: {"swing_start": 1000.0 if t < 500.0 else 1400.0})
    shot = SimpleNamespace(ticker=TICKER, series="KXBTC15M", side="yes")
    hours = 0.1
    assert priskp.swing_played(shot, 600.0, hours, [100.0]) is True  # the earlier bet was in the swing that started at 1000: the same one
    assert priskp.swing_played(shot, 600.0, hours, [100.0, 700.0]) is True
    assert priskp.swing_played(shot, 600.0, hours, [700.0]) is False  # that bet was in a swing that started at 1400: a different one
    assert priskp.swing_played(shot, 600.0, hours, []) is False  # nothing bought yet
    cur["swing_start"] = 1700.0  # the swing has turned: a new one
    assert priskp.swing_played(shot, 600.0, hours, [100.0]) is False
    cur["swing_start"] = 1000.0
    cur["regime"] = "steady"
    assert priskp.swing_played(shot, 600.0, hours, [100.0]) is False  # only in a curve
