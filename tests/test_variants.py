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
