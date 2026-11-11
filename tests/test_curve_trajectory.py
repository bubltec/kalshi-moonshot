from collections import deque
from types import SimpleNamespace

from kalshi_moonshot import curvevalue, watcher

RATE = 1e-4  # per sqrt-second; a typical minute is RATE * sqrt(60)
UNIT = RATE * 60**0.5


def target(**kw):
    base = dict(ticker="KXBTC15M-1", lane="x", side="yes", curve_rest=-1.0, curve_comeback=-1.0, curve_comeback_min=5, curve_comeback_hold=240.0, comeback_noted=0.0, hold_since=0.0, hold_ref=0.0, hold_fall=0.0, curve_margin=0.15, curve_persist=5.0, curve_scales="900", curve_last=900.0, curve_since=0.0, curve_at=0.0, curve_noted=0.0,
                close_ts=9_850.0, shock_ref=100.0, shock_index="BRTI", curve_drop_units=1.5, curve_drop_seconds=60.0, curve_drop_margin=0.5, curve_drop_persist=0.0, curve_drop_since=0.0, curve_cush=deque(), curve_gap=-1.0, curve_gap_persist=15.0, curve_gap_since=0.0, curve_behind_last=-1.0, curve_behind_chance=0.5, curve_near=-1.0)
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


def test_time_to_go_sells_a_position_behind_the_strike_only_in_its_last_seconds(monkeypatch):
    # close_ts 9850, readings start at 9000: 850 s left falling a second at a time
    path = [-1.0] * 846
    i, reason = run(monkeypatch, target(curve_drop_units=-1.0, curve_behind_last=240.0), path, bid=0.03, p=0.20)
    assert reason and reason.startswith("curve exit (time to go)") and i == 610  # 240 s left, though the bid is under the other sales' floor
    assert run(monkeypatch, target(curve_drop_units=-1.0, curve_behind_last=240.0), path, bid=0.60, p=0.70)[1] is None  # ahead of the strike: held
    assert run(monkeypatch, target(curve_drop_units=-1.0), path, bid=0.03, p=0.20)[1] is None  # off by default


def test_time_to_go_waits_for_its_own_chance_cutoff(monkeypatch):
    path = [-0.5] * 846
    assert run(monkeypatch, target(curve_drop_units=-1.0, curve_behind_last=30.0, curve_behind_chance=0.3), path, bid=0.30, p=0.40)[1] is None  # behind, but not under 0.3
    i, reason = run(monkeypatch, target(curve_drop_units=-1.0, curve_behind_last=30.0, curve_behind_chance=0.3), path, bid=0.10, p=0.20)
    assert reason and "under 0.3" in reason and i == 820


def test_the_curve_sale_margin_shrinks_toward_the_close(monkeypatch):
    # bid 0.90 (about 0.892 after the fee) against a chance of 0.82: about 0.07 over
    path = [2.0] * 846  # 850 s left at the start, one reading a second
    assert run(monkeypatch, target(curve_drop_units=-1.0), path, bid=0.90, p=0.82)[1] is None  # fixed 0.15: never
    i, reason = run(monkeypatch, target(curve_drop_units=-1.0, curve_near=0.0), path, bid=0.90, p=0.82)
    assert reason and reason.startswith("curve exit: bid") and "s left" in reason
    left = 850 - i
    assert 150 < left < 230  # 0.15 * sqrt(left / 900) falls to about 0.07 near 200 s left, then 5 s of persistence
    i2, _ = run(monkeypatch, target(curve_drop_units=-1.0, curve_near=0.05), path, bid=0.90, p=0.82)
    assert i2 > i  # a floor of 0.05 at the close waits longer


def _hold_run(monkeypatch, path, rate=(0.6, 10), **kw):
    """Feed one cushion reading a second through a lane with the comeback hold; return (second, reason) of the first sale, and the target."""
    monkeypatch.setattr(watcher.log, "info", lambda *a, **k: None)
    monkeypatch.setattr(watcher.variants, "comeback_rate", lambda *a, **k: rate)
    monkeypatch.setattr(watcher.ticks_mod.history, "window", lambda *a, **k: ([0.0], [1.0]))
    me = SimpleNamespace(COMEBACK_BACK_SECONDS=watcher.ProfitWatcher.COMEBACK_BACK_SECONDS)
    me._comeback_holds = lambda *a: watcher.ProfitWatcher._comeback_holds(me, *a)
    me._comeback_expired = lambda *a: watcher.ProfitWatcher._comeback_expired(me, *a)
    t = target(curve_comeback=0.5, close_ts=kw.pop("close_ts", 9_900.0), **kw)
    state = {}
    monkeypatch.setattr(curvevalue, "read", lambda *a, **k: curvevalue.Curve(0.45, state["tm"] * UNIT, RATE, RATE))
    for i, tm in enumerate(path):
        state["tm"] = tm
        now = 9_000.0 + i
        reason = watcher.ProfitWatcher._curve_hit(me, t, 0.40, now, now)
        if reason:
            return i, reason, t
    return None, None, t


DIP = [2.5] * 30 + [2.5 - 2.3 * k / 60 for k in range(61)]  # a fall of 2.3 typical minutes in a minute, to +0.2


def test_the_hold_lets_a_dip_come_back_without_selling_it(monkeypatch):
    """The XRP sale of 2026-11-11 12:08: a one-minute dip to the strike, back to 0.70 thirty seconds later, and the market won."""
    _, reason, t = _hold_run(monkeypatch, DIP + [0.2 + 1.6 * k / 30 for k in range(31)] + [1.8] * 60)
    assert reason is None and t.hold_since == 0.0  # held, regained more than half, ended with no sale


def test_the_hold_sells_a_dip_that_does_not_come_back_after_120_seconds(monkeypatch):
    _, reason, _ = _hold_run(monkeypatch, DIP + [0.2] * 200)
    assert reason and reason.startswith("curve exit (trajectory, comeback hold over)")
    held = int(reason.split("no comeback in ")[1].split("s")[0])
    assert 120 <= held <= 121  # called when the 120 s the rate was measured over have passed


def test_the_hold_sells_at_once_when_the_dip_turns_into_a_slide(monkeypatch):
    i, reason, t = _hold_run(monkeypatch, DIP + [0.2 - 2.4 * k / 40 for k in range(41)] + [-2.2] * 60)
    assert reason and "kept falling" in reason and i < 60 + 30 + 60


def test_the_hold_does_not_run_in_the_last_240_seconds_and_ends_there(monkeypatch):
    # 200 s left when the dip arrives: no hold at all, the trajectory sale fires as before
    i, reason, t = _hold_run(monkeypatch, DIP + [0.2] * 20, close_ts=9_000.0 + 90.0 + 200.0)
    assert reason and reason.startswith("curve exit (trajectory):")
    # a hold begun with time to spare is called when 240 s are left and the cushion is still down
    i, reason, t = _hold_run(monkeypatch, DIP + [0.2] * 200, close_ts=9_000.0 + 90.0 + 300.0)
    assert reason and reason.startswith("curve exit (trajectory, comeback hold over)")


def test_the_hold_needs_a_good_rate_and_enough_falls_to_start(monkeypatch):
    for rate in ((0.25, 8), (0.6, 3), None):
        i, reason, t = _hold_run(monkeypatch, DIP + [0.2] * 20, rate=rate)
        assert reason and reason.startswith("curve exit (trajectory):"), rate
