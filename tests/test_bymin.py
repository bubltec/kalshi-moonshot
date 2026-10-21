from kalshi_moonshot import journal
from kalshi_moonshot.journal import Trade


def bet(minute_in, pnl, close=10_000.0):
    # opened `minute_in` minutes into a 15-minute window that closes at `close`
    opened = close - (15 - minute_in) * 60
    return Trade("KXBTC15M-1", "late_ticket", "KXBTC15M", "yes", False, 1.0, 1.0 + pnl, close, False, opened, close)


def test_bets_are_grouped_by_the_minute_they_were_placed():
    rows = journal.by_entry_minute([bet(1, -1.0), bet(2, 2.0), bet(7, -1.0), bet(13, 3.0)])
    got = {band: (n, won, round(pnl, 2)) for band, n, won, staked, pnl in rows}
    assert got == {"minutes 0-3": (2, 1, 1.0), "minutes 6-9": (1, 0, -1.0), "minutes 12-15": (1, 1, 3.0)}


def test_non_15_minute_and_untimed_bets_are_left_out():
    other = Trade("KXBTCD-1", "steady", "KXBTCD", "yes", False, 1.0, 2.0, 5.0, False, 0.0, 0.0)
    assert journal.by_entry_minute([other]) == []
