from collections import deque
from types import SimpleNamespace

from kalshi_moonshot import curvevalue, watcher

RATE = 1e-4  # per sqrt-second; a typical minute is RATE * sqrt(60)
UNIT = RATE * 60**0.5


def target(**kw):
    base = dict(ticker="KXBTC15M-1", lane="x", side="yes", curve_margin=0.15, curve_persist=5.0, curve_scales="900", curve_last=900.0, curve_since=0.0, curve_at=0.0, curve_noted=0.0,
                close_ts=9_850.0, shock_ref=100.0, shock_index="BRTI", curve_drop_units=1.5, curve_drop_seconds=60.0, curve_drop_margin=0.5, curve_drop_persist=0.0, curve_drop_since=0.0, curve_cush=deque(), curve_gap=-1.0, curve_gap_persist=15.0, curve_gap_since=0.0)
    base.update(kw)
    return SimpleNamespace(**base)


def run(monkeypatch, t, path, bid=0.40, p=0.45):
    """Feed one cushion reading (in typical minutes) a second; return the first sale reason and when."""
    monkeypatch.setattr(watcher.ticks_mod.history, "window", lambda *a, **k: ([0.0], [1.0]))
    state = {}
    monkeypatch.setattr(curvevalue, "read", lambda *a, **k: curvevalue.Curve(p, state["tm"] * UNIT, RATE, RATE))
    for i, tm in enumerate(path):
        state["tm"] = tm
        now = 9_000.0 + i
        reason = watcher.ProfitWatcher._curve_hit(None, t, bid, now, now)
        if reason:
            return i, reason
    return None, None


def test_a_cushion_that_drains_fast_to_the_strike_is_sold_though_the_bid_is_under_the_curve(monkeypatch):
    path = [2.5] * 30 + [2.5 - 2.3 * k / 60 for k in range(61)] + [0.2] * 20  # +2.5 to +0.2 in a minute
    i, reason = run(monkeypatch, target(), path)
    assert reason and reason.startswith("curve exit (trajectory)") and 60 <= i <= 95


def test_a_fall_that_leaves_a_thick_cushion_or_a_slow_drift_is_held(monkeypatch):
    assert run(monkeypatch, target(), [4.0] * 30 + [4.0 - 2.0 * k / 60 for k in range(61)] + [2.0] * 30)[1] is None  # still 2 typical minutes ahead
    assert run(monkeypatch, target(), [1.2 - 1.0 * k / 600 for k in range(600)])[1] is None  # one unit over ten minutes


def test_the_trajectory_sale_is_off_by_default_and_waits_out_its_persistence(monkeypatch):
    path = [2.5] * 30 + [2.5 - 2.3 * k / 60 for k in range(61)] + [0.2] * 5
    assert run(monkeypatch, target(curve_drop_units=-1.0), path)[1] is None
    i0, _ = run(monkeypatch, target(), path)
    i5, r5 = run(monkeypatch, target(curve_drop_persist=5.0), path)
    assert r5 and i5 >= i0 + 5


def test_the_value_sale_still_fires_on_its_own(monkeypatch):
    i, reason = run(monkeypatch, target(curve_drop_units=-1.0), [2.0] * 20, bid=0.80, p=0.50)
    assert reason and reason.startswith("curve exit: bid") and i >= 5


def test_a_bid_far_under_the_curve_chance_is_sold_after_its_persistence(monkeypatch):
    # our spot says 0.95, the market bids 0.55: the reading is in doubt
    i, reason = run(monkeypatch, target(curve_drop_units=-1.0, curve_gap=0.35), [3.0] * 40, bid=0.55, p=0.95)
    assert reason and reason.startswith("curve exit (market disagrees)") and 15 <= i <= 17
    assert run(monkeypatch, target(curve_drop_units=-1.0, curve_gap=0.35), [3.0] * 40, bid=0.70, p=0.95)[1] is None  # a gap under the level is held
    assert run(monkeypatch, target(curve_drop_units=-1.0), [3.0] * 40, bid=0.55, p=0.95)[1] is None  # off by default
