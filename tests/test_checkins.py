from kalshi_moonshot import checkins


def test_a_mark_is_recorded_once_as_the_clock_passes_it():
    seen: set[int] = set()
    assert checkins.due(300, seen) is None  # before the first mark
    assert checkins.due(239, seen) == 240 and checkins.due(238, seen) is None
    assert checkins.due(209.5, seen) == 210
    assert checkins.due(5, seen) == 10  # the last look
    assert checkins.due(0, seen) is None


def test_a_late_start_or_a_stall_records_nothing_for_marks_it_missed():
    seen: set[int] = set()
    assert checkins.due(100, seen) is None  # a position found at 100s: 240 down to 120 are past, none within 10s of their mark
    assert checkins.due(88, seen) == 90  # but the next mark is still taken on time
    seen = set()
    assert checkins.due(139, seen) is None  # a stalled check: 240..150 missed, 150 is 11s late
    assert checkins.due(121, seen) is None
    assert checkins.due(119, seen) == 120


def test_side_through_is_positive_on_the_losing_side():
    assert checkins.side_through("yes", 100.0, 99.99) > 0 and checkins.side_through("yes", 100.0, 100.5) < 0
    assert checkins.side_through("no", 100.0, 100.01) > 0 and checkins.side_through("no", 100.0, 99.5) < 0
    assert checkins.side_through("yes", 0.0, 99.0) == 0.0

