"""The last hour of every market's quotes, in memory (ticks.MidHistory; 2026-11-11, user: "every market ticket should have the past 15 minutes, if we could store the past hour even better ... in memory")."""

import pytest

from kalshi_moonshot import ticks


def test_a_market_keeps_an_hour_one_row_a_second_with_bid_and_ask():
    h = ticks.MidHistory()
    for k in range(4000):
        h.note("KXBTC15M-26NOV111200-00", 1000.0 + k * 0.5, 0.50, 0.52)  # two quotes a second
    rows = h.rows("KXBTC15M-26NOV111200-00")
    assert len(rows) <= 3601 and rows[-1][0] - rows[0][0] <= 3600.0  # an hour, not 15 minutes
    assert len({int(t) for t, _ in rows}) == len(rows)  # one row a second
    assert h.quotes("KXBTC15M-26NOV111200-00")[-1][1:] == (0.50, 0.52)
    assert rows[-1][1] == 0.51


def test_the_readings_that_used_the_mids_are_unchanged():
    h = ticks.MidHistory()
    for k in range(400):
        h.note("T", 1000.0 + k, 0.40 if (k // 20) % 2 == 0 else 0.60, 0.42 if (k // 20) % 2 == 0 else 0.62)  # the mid crosses 0.5 every 20 s
    assert h.crossings("T", 1399.0, seconds=300.0) >= 12
    assert h.rows("T")[0] == (1000.0, pytest.approx(0.41))


def test_a_series_path_stitches_its_windows_in_time_order():
    h = ticks.MidHistory()
    for k in range(300):
        h.note("KXETH15M-26NOV111145-45", 1000.0 + k, 0.40, 0.42)
    for k in range(300):
        h.note("KXETH15M-26NOV111200-00", 1300.0 + k, 0.60, 0.62)
    h.note("KXBTC15M-26NOV111200-00", 1300.0, 0.10, 0.12)
    path = h.series_rows("KXETH15M", 1600.0, 3600.0)
    assert len(path) == 600 and path[0][0] == 1000.0 and path[-1][0] == 1599.0 and path[299][1] == pytest.approx(0.41) and path[300][1] == pytest.approx(0.61)
    assert h.series_rows("KXETH15M", 1600.0, 100.0)[0][0] >= 1500.0


def test_closed_windows_do_not_pile_up():
    h = ticks.MidHistory(keep=3600.0)
    for w in range(30):
        h.note(f"KXBTC15M-W{w}", 1000.0 + w * 900.0, 0.5, 0.52)
    assert h.prune(1000.0 + 30 * 900.0) == 26  # only the windows whose last quote is inside the hour remain
    assert len(h._rows) == 4


def test_a_restart_fills_the_start_of_an_open_market_from_the_tape_and_keeps_live_rows():
    h = ticks.MidHistory()
    for k in range(60):
        h.note("T", 2000.0 + k, 0.55, 0.57)  # what arrived since the restart
    added = h.warm({"T": [(1000.0 + k, 0.30, 0.32) for k in range(1000)] + [(2010.0, 0.99, 0.99)], "NEW": [(1500.0, 0.2, 0.22)]})
    rows = h.rows("T")
    assert added == 1001 and rows[0] == (1000.0, pytest.approx(0.31)) and rows[999][0] == 1999.0 and rows[1000] == (2000.0, pytest.approx(0.56))  # tape rows in front, live rows untouched
    assert all(r[1] != 0.99 for r in rows)  # a tape quote at or after the first live row is not added
    assert h.rows("NEW") == [(1500.0, pytest.approx(0.21))]
