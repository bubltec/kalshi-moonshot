"""The side's own PRICE swings (wavefacts.price_swings) and the curve entry's wait while the price is still climbing (priskp._price_rising; priskp.screen). 2026-11-27, user, on XRP 06:30 YES bought at 0.79 sixteen seconds after the lane sold the same side at 0.78 at the top of its climb: "The peak reading looks at the spot's swings ... this is a bug we need to fix"; "I'd rather have that than a wait. The wait is a patch fix. This is a real fix"; "Ideally it shouldn't need a wait. The wait should just happen"."""
from types import SimpleNamespace

from kalshi_moonshot import priskp, ticks, wavefacts

OPEN = 2_000_000.0


def path(legs, step=2.0, start=0.50):
    """(time, YES mid) rows along straight legs [(seconds, change)] from the open."""
    rows, t, v = [], OPEN, start
    for seconds, change in legs:
        for k in range(1, int(seconds / step) + 1):
            rows.append((t + k * step, v + change * k * step / seconds))
        t, v = t + seconds, v + change
    return rows, t


# a climb to 0.78, a fall to 0.60, a climb to 0.86 (the price's own swings: 0.28, 0.18, 0.26)
BASE = [(240, 0.28), (240, -0.18), (180, 0.26)]


def test_a_price_still_climbing_at_its_top_is_rising_and_one_that_has_pulled_back_is_not():
    rows, now = path(BASE + [(30, 0.0)])
    top = wavefacts.price_swings(rows, OPEN, now, "yes")
    assert top is not None and top["rising"] is True and top["dirs"] == [1, 1]
    rows2, now2 = path(BASE + [(40, -0.05)])  # the price gives back 0.05 (a fifth of its last swing)
    pulled = wavefacts.price_swings(rows2, OPEN, now2, "yes")
    assert pulled["rising"] is False and 1 in pulled["dirs"] or pulled["dirs"][1] == -1  # the small level has turned down
    assert pulled["rising"] is False


def test_the_no_side_reads_one_minus_the_mid():
    rows, now = path(BASE + [(30, 0.0)])
    assert wavefacts.price_swings(rows, OPEN, now, "no")["rising"] is False  # the YES price is at its top: the NO price is at its bottom
    flipped = [(t, 1.0 - m) for t, m in rows]
    assert wavefacts.price_swings(flipped, OPEN, now, "no")["rising"] is True  # a NO side whose own price climbs


def test_no_reading_with_too_few_quotes_or_a_path_that_never_turned():
    rows, now = path([(20, 0.05)])
    assert wavefacts.price_swings(rows, OPEN, now, "yes") is None  # under twelve grid points
    rows, now = path([(300, 0.10)])
    assert wavefacts.price_swings(rows, OPEN, now, "yes") is None  # one straight climb: nothing to size a swing by
    assert wavefacts.price_swings([], OPEN, OPEN + 300.0, "yes") is None
    assert wavefacts.price_swings(rows, OPEN, now, "maybe") is None


def test_the_lane_reads_the_markets_own_quotes_from_memory_and_waits_at_the_price_peak(monkeypatch):
    """The re-buy: the lane sold at the top of the climb and the price is still there. No timer: the same reading says wait now and buy once the price has pulled back."""
    ticks.mids.clear()
    tk = "KXXRP15M-26NOV270630-30"
    rows, now = path(BASE + [(30, 0.0)])
    for t, m in rows:
        ticks.mids.note(tk, t, m - 0.005, m + 0.005)
    shot = SimpleNamespace(ticker=tk, side="yes")
    hours = (OPEN + 900.0 - now) / 3600.0
    assert priskp._price_rising(shot, now, hours) is True  # still at its top
    for k in range(1, 22):  # twenty more seconds: the price gives back a fifth of its last swing
        t = now + k
        ticks.mids.note(tk, t, 0.86 - 0.0025 * k * 2 - 0.005, 0.86 - 0.0025 * k * 2 + 0.005)
    assert priskp._price_rising(shot, now + 22.0, hours - 22.0 / 3600.0) is False  # pulled back at the small level: the wait has ended
    assert priskp._price_rising(SimpleNamespace(ticker="KXNONE15M-26NOV270630-30", side="yes"), now, hours) is None  # no quotes: nothing is held back
    ticks.mids.clear()
