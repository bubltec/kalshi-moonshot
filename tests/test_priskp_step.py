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
    assert facts.NEEDS["priskp"] == ("priskp_screen", "shift_out", "hour_view", "price_gap", "waves_before", "wave_now", "wave_leg") and all(n in facts.READERS for n in facts.NEEDS["priskp"])  # the wave facts (2026-11-25) are on the board for every step to read


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
