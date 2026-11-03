import math

import pytest

from kalshi_moonshot import variants


def test_each_variety_is_read_from_the_cushion_the_slope_and_the_crossings():
    assert variants.classify(2.0, 0.9, 0) == "A"  # far and still moving away
    assert variants.classify(2.0, 0.0, 1) == "B"  # far and flat
    assert variants.classify(1.5, -0.8, 2) == "C"  # far but coming back
    assert variants.classify(0.4, -0.7, 1) == "D"  # near and approaching the strike
    assert variants.classify(0.4, 0.1, 0) == "E"  # near and flat
    assert variants.classify(0.4, 0.9, 3) == "F"  # near but pulling away
    assert variants.classify(2.0, 0.9, 4) == "G"  # four crossings is oscillation whatever the cushion


def _ticks(values, start=1000.0):
    return [start + k for k in range(len(values))], list(values)


def test_a_snapshot_reads_a_steady_cushion_as_far_and_flat_and_flips_with_the_side():
    wobble = [100.0 * (1 + 0.0004 * (1 if k % 2 else -1) * 0.1) + 0.2 for k in range(400)]  # 20 bp above a strike of 100, barely moving
    ts, v = _ticks(wobble)
    at = ts[-1]
    close = at + 300.0
    yes = variants.snapshot(ts, v, 100.0, "yes", at, close)
    assert yes is not None and yes["variety"] == "B" and yes["crosses"] == 0 and yes["z"] > 1.0
    no = variants.snapshot(ts, v, 100.0, "no", at, close)
    assert no is not None and no["z"] == pytest.approx(-yes["z"], rel=1e-9) and no["variety"] in ("D", "E", "F")


def test_a_snapshot_counts_the_crossings_and_refuses_thin_or_stale_history():
    ts, v = _ticks([100.0 + (0.05 if (k // 20) % 2 else -0.05) for k in range(400)])  # crosses the strike every 20 s
    snap = variants.snapshot(ts, v, 100.0, "yes", ts[-1], ts[-1] + 300.0)
    assert snap is not None and snap["variety"] == "G" and snap["crosses"] >= 4
    assert variants.snapshot(ts[:5], v[:5], 100.0, "yes", ts[4], ts[4] + 300.0) is None  # too few ticks
    assert variants.snapshot(ts, v, 100.0, "yes", ts[-1] + 60.0, ts[-1] + 360.0) is None  # the last tick is a minute old
    assert variants.snapshot(ts, v, 0.0, "yes", ts[-1], ts[-1] + 300.0) is None  # no strike
    assert not math.isnan(snap["z"])


def test_the_favorite_is_the_first_qualifying_side_between_minute_seven_and_the_last_two_minutes():
    close = 1000.0
    quotes = [
        (close - 600, 0.84, 0.85),  # before minute 7: too early
        (close - 470, 0.60, 0.62),  # no side in the band
        (close - 400, 0.14, 0.15),  # the NO side asks 0.86 with a 1c spread
        (close - 300, 0.86, 0.87),
        (close - 100, 0.90, 0.91),  # inside the last two minutes: too late
    ]
    side, ask, at, bid = variants.favorite_entry(quotes, close)
    assert side == "no" and ask == pytest.approx(0.86) and at == close - 400 and bid == pytest.approx(0.85)
    assert variants.favorite_entry([(close - 300, 0.80, 0.85)], close) is None  # a 5c spread is too wide
    assert variants.favorite_entry([(close - 300, 0.95, 0.96)], close) is None  # above the band
    assert variants.favorite_entry([], close) is None


def test_the_grid_makes_a_slow_and_a_fast_feed_read_alike():
    def path(t):  # one spot path: 1 bp wiggles that cross the strike of 100 every 20 s
        return 100.0 + (0.01 if (t // 20) % 2 else -0.01)

    fast = [(1000.0 + k, path(k)) for k in range(400)]  # a tick every second
    slow = [(1000.0 + k, path(k)) for k in range(0, 400, 3)]  # the same path, a tick every 3 s
    a = variants.snapshot([x for x, _ in fast], [y for _, y in fast], 100.0, "yes", 1399.0, 1699.0)
    b = variants.snapshot([x for x, _ in slow], [y for _, y in slow], 100.0, "yes", 1398.0, 1698.0)
    assert a is not None and b is not None
    assert a["variety"] == b["variety"] == "G" and abs(a["crosses"] - b["crosses"]) <= 1


def test_a_skip_list_names_a_variety_on_either_side_or_on_one():
    assert variants.skipped(("G", "A-yes"), "G", "no") and variants.skipped(("G", "A-yes"), "G", "yes")
    assert variants.skipped(("G", "A-yes"), "A", "yes") and not variants.skipped(("G", "A-yes"), "A", "no")
    assert not variants.skipped(("G", "A-yes"), "B", "yes")
    assert not variants.skipped(("G",), None, "yes")  # a variety that cannot be read is never skipped
