"""The nested-swings reading (wavefacts.nest; user, 2026-11-26: "Don't build on waves that are specific lengths"; "Why doesn't it read before the markets open?"; "Ok I like that"): the market's own swings by size from the hour before the open on, the bet's side with or against each, and the shadow note the PriskP look carries. It decides nothing."""
from types import SimpleNamespace

import pytest

from kalshi_moonshot import binaryrisk, memguard, priskp, ticks, wavefacts

OPEN = 1_800_000_000.0 - (1_800_000_000 % 900)  # a quarter-hour boundary


def feed(index, legs, start=OPEN - 3600.0, v0=100.0):
    """Ticks every second along straight legs [(seconds, change), ...] from an hour before the open."""
    t, v = start, v0
    ticks.history.note(index, t - 30.0, v)
    for seconds, change in legs:
        for k in range(1, int(seconds) + 1):
            ticks.history.note(index, t + k, v + change * k / seconds)
        t, v = t + seconds, v + change
    return t


@pytest.fixture(autouse=True)
def clean():
    ticks.history.clear()
    wavefacts._nest.clear()
    wavefacts._before.clear()
    yield
    ticks.history.clear()
    wavefacts._nest.clear()
    wavefacts._before.clear()


# the hour before the open: up 10 over 30 minutes, down 6 over 20, up 2 over 10; then, in the market, down 0.5 over 3 minutes
LEGS = [(1800, 10.0), (1200, -6.0), (600, 2.0), (180, -0.5)]


def test_the_swings_are_read_by_size_from_the_hour_before_the_open_whatever_their_length():
    now = feed("NEST", LEGS)
    assert now == OPEN + 180.0
    with memguard.decision("buy"):
        yes = wavefacts.nest("NEST", OPEN, now, "yes")
        no = wavefacts.nest("NEST", OPEN, now, "no")
    # the biggest reversal the path shows is the 6 fall: at that size and at half of it the current swing is DOWN (the 2 rise is under both);
    # at a quarter (1.5) the 2 rise counts and the 0.5 dip does not: UP; at an eighth (0.75) the same
    assert yes["signs"] == "- - + +" and yes["with"] == 2
    assert no["signs"] == "+ + - -" and no["with"] == 2
    assert yes["levels"] == [-1, -1, 1, 1]


def test_a_ripple_at_minute_three_is_read_inside_the_move_the_market_opened_in():
    """BTC 22:30 NO, 2026-11-26, in shape: a long climb into the open, then a small dip. `leg` saw only the dip; the nest says the two larger swings run against a NO bet."""
    now = feed("NEST", [(2400, -3.0), (1200, 8.0), (180, -0.6)])
    no = wavefacts.nest("NEST", OPEN, now, "no")
    assert no["levels"][0] == -1 and no["levels"][1] == -1  # the climb is the current swing at the biggest size and at half of it
    assert wavefacts.nest("NEST", OPEN, now, "yes")["with"] >= 2


def test_no_reading_without_the_hour_before_the_open_or_without_a_turn():
    feed("SHORT", [(600, 1.0)], start=OPEN - 600.0)
    assert wavefacts.nest("SHORT", OPEN, OPEN + 60.0, "yes") is None  # the memory does not reach an hour back
    feed("LINE", [(3780, 5.0)])
    assert wavefacts.nest("LINE", OPEN, OPEN + 180.0, "yes") is None  # one straight line: the path never turned
    assert wavefacts.nest("NEST", OPEN, OPEN + 180.0, "maybe") is None


def test_integration_the_priskp_look_carries_the_nest_as_a_shadow_note_and_says_what_it_would_do(monkeypatch):
    index = binaryrisk.curve_index("KXBTC15M")
    assert index
    now = feed(index, LEGS)
    shot = SimpleNamespace(series="KXBTC15M", ticker="KXBTC15M-26NOV262230-30", side="yes")
    hours = (OPEN + 900.0 - now) / 3600.0
    with memguard.decision("buy"):
        looking = priskp._nest_note(shot, now, hours, False)
        buying = priskp._nest_note(shot, now, hours, True)
    assert looking.startswith("- - + + (2 of 4 with the bet") and "shadow" not in looking  # a look that is not a buy only records the reading
    assert buying.endswith("shadow: would WAIT (a bounce against the larger move)")
    monkeypatch.setattr(wavefacts, "nest", lambda *a: {"signs": "- + + +", "with": 3, "biggest_u": 6.4, "levels": [-1, 1, 1, 1]})
    assert priskp._nest_note(shot, now, hours, True).endswith("the biggest swing in view 6.4 u); shadow: would buy too")
    monkeypatch.setattr(wavefacts, "nest", lambda *a: {"signs": "+ + + +", "with": 4, "biggest_u": None, "levels": [1, 1, 1, 1]})
    assert priskp._nest_note(shot, now, hours, True).endswith("shadow: would WAIT (the move has fully run)")
    monkeypatch.setattr(wavefacts, "nest", lambda *a: None)
    assert priskp._nest_note(shot, now, hours, True) is None


def test_the_leg_walked_from_before_the_open_knows_a_move_that_began_before_it():
    """User, 2026-11-27: "Why doesn't it read before the markets open? It buys starting at 3 mins how would it know if it's not inside a curve?"; "Can we make it look at least 10mins back?". A climb that began 27 minutes before the open and is still going at minute 4: walked from the open it is a young swing that has travelled little; walked from before the open it has already run past a usual leg and is at its extreme."""
    import random

    rnd = random.Random(7)
    t, v = OPEN - 3600.0, 100.0
    ticks.history.note("LEGX", t - 30.0, v)
    for seconds, change in [(500, 4.0), (500, -4.0), (500, 4.0), (500, -4.0), (1600, 7.0), (240, 1.0)]:  # four big legs in the hour, then the climb through the open
        for k in range(1, seconds + 1):
            ticks.history.note("LEGX", t + k, v + change * k / seconds + rnd.uniform(-0.02, 0.02))
        t, v = t + seconds, v + change
    now = OPEN + 240.0
    with memguard.decision("buy"):
        short = wavefacts.leg("LEGX", OPEN, now, 106.0, "yes", 660.0)
        long = wavefacts.leg("LEGX", OPEN, now, 106.0, "yes", 660.0, wavefacts.HOUR)
        ten = wavefacts.leg("LEGX", OPEN, now, 106.0, "yes", 660.0, 600.0)
    assert short["class"] == long["class"] == ten["class"] == "inside"  # the same direction either way
    assert not short["at_extreme"] and short["travel"] < 1.0  # from the open: a young swing that has travelled under half a usual wave, bought
    assert long["at_extreme"] and long["travel"] > short["travel"] * 3  # from before the open: it has travelled more than three usual waves
    assert ten["at_extreme"] and ten["travel"] > 1.0  # ten minutes back already sees it
