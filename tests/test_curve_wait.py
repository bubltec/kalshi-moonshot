"""The curve sale and time-to-go wait by the market's own recovery time, less the deeper the drop, never over a share of the time left (lane field curve_wait_share; 2026-11-11, user: "the exit wait needs to scale with time left and how severe a drop. in this case it barely fell below the target")."""

from collections import deque
from types import SimpleNamespace

from kalshi_moonshot import curvevalue, watcher

RATE = 1e-4
UNIT = RATE * 60**0.5
CLOSE = 9_900.0


def target(**kw):
    base = dict(ticker="KXWTI15M-1", lane="x", side="yes", curve_rest=-1.0, curve_wait_share=0.25, curve_wait_severe=1.5, curve_wait_default=15.0, go_since=0.0, curve_comeback=-1.0, curve_comeback_min=5,
                curve_comeback_hold=240.0, comeback_noted=0.0, hold_since=0.0, hold_ref=0.0, hold_fall=0.0, curve_margin=0.06, curve_near=-1.0, curve_persist=15.0, curve_scales="900", curve_last=900.0, curve_since=0.0,
                curve_at=0.0, curve_noted=0.0, close_ts=CLOSE, shock_ref=100.0, shock_index="BQ_WTI", curve_drop_units=-1.0, curve_drop_seconds=60.0, curve_drop_margin=0.5, curve_drop_persist=0.0, curve_drop_since=0.0,
                curve_cush=deque(), curve_gap=-1.0, curve_gap_persist=15.0, curve_gap_since=0.0, curve_behind_last=240.0, curve_behind_chance=0.5)
    base.update(kw)
    return SimpleNamespace(**base)


def me():
    o = SimpleNamespace()
    o._scaled_wait = lambda *a: watcher.ProfitWatcher._scaled_wait(o, *a)
    o._comeback_expired = lambda *a: None
    return o


def stats(monkeypatch, got):
    monkeypatch.setattr(watcher.variants, "comeback_stats", lambda *a, **k: got)


def test_the_wait_is_the_markets_own_recovery_time_cut_by_depth_and_capped_by_the_time_left(monkeypatch):
    stats(monkeypatch, (0.6, 10, 40.0))
    t, w = target(), me()
    assert w._scaled_wait(t, 0.0, 600.0, 0.5) == 40.0  # above the target, plenty of time: the whole recovery time
    assert abs(w._scaled_wait(t, 0.0, 600.0, -0.75) - 20.0) < 1e-9  # half way to the severe depth: half the wait
    assert w._scaled_wait(t, 0.0, 600.0, -1.5) == 0.0 and w._scaled_wait(t, 0.0, 600.0, -3.0) == 0.0  # deep under the target: no wait
    assert w._scaled_wait(t, 0.0, 100.0, 0.5) == 25.0  # a quarter of the time left caps it
    assert w._scaled_wait(t, 0.0, 12.0, 0.5) == 3.0


def test_with_too_few_falls_in_the_hour_the_default_recovery_time_is_used(monkeypatch):
    for got in ((0.9, 3, 5.0), None):
        stats(monkeypatch, got)
        assert me()._scaled_wait(target(), 0.0, 600.0, 0.5) == 15.0


def run(monkeypatch, t, steps, bid=0.48, p=0.30, rate=(0.6, 10, 40.0)):
    """One reading a second from 9,000 s; steps are cushions in typical minutes. Returns (second, reason) of the first sale."""
    monkeypatch.setattr(watcher.ticks_mod.history, "window", lambda *a, **k: ([0.0], [1.0]))
    monkeypatch.setattr(watcher.log, "info", lambda *a, **k: None)
    stats(monkeypatch, rate)
    state = {}
    monkeypatch.setattr(curvevalue, "read", lambda *a, **k: curvevalue.Curve(state["p"], state["tm"] * UNIT, RATE, RATE))
    w = me()
    for i, tm in enumerate(steps):
        state["tm"], state["p"] = tm, p
        now = 9_000.0 + i
        reason = watcher.ProfitWatcher._curve_hit(w, t, bid, now, now)
        if reason:
            return i, reason
    return None, None


def test_the_curve_sale_waits_the_scaled_time_not_the_fixed_persistence(monkeypatch):
    # a shallow dip (cushion +0.4) with 900 s left: the wait is 40 s (the market's recovery time), not the lane's fixed 15 s
    i, reason = run(monkeypatch, target(close_ts=9_900.0), [0.4] * 80)
    assert reason and reason.startswith("curve exit: bid") and 40 <= i <= 42, i
    # the old fixed persistence still applies when the lane has not turned the wait on
    i, reason = run(monkeypatch, target(curve_wait_share=-1.0), [0.4] * 80)
    assert reason and 15 <= i <= 17, i


def test_a_drop_well_under_the_target_is_sold_at_once_by_the_curve_sale(monkeypatch):
    i, reason = run(monkeypatch, target(), [-2.0] * 40)
    assert reason and i <= 1  # severity at or over the severe depth: no wait beyond the one-second reading


def test_time_to_go_waits_for_a_shallow_miss_but_not_for_a_deep_one(monkeypatch):
    # 200 s left, chance 0.3, cushion -0.3: the wait is min(40 * 0.8, a quarter of the time left = 50) = 32 s
    i, reason = run(monkeypatch, target(close_ts=9_200.0), [-0.3] * 80, bid=0.40)
    assert reason and reason.startswith("curve exit (time to go)") and 30 <= i <= 34, (i, reason)
    i, reason = run(monkeypatch, target(close_ts=9_200.0), [-2.5] * 40, bid=0.40)
    assert reason and reason.startswith("curve exit (time to go)") and i <= 1
    # off: it sells at once, as before
    i, reason = run(monkeypatch, target(close_ts=9_200.0, curve_wait_share=-1.0), [-0.3] * 40, bid=0.40)
    assert reason and reason.startswith("curve exit (time to go)") and i <= 1


def test_a_recovery_resets_the_time_to_go_clock(monkeypatch):
    path = [-0.3] * 20 + [0.8] * 5 + [-0.3] * 80  # the cushion comes back above the cut for a few seconds
    monkeypatch.setattr(watcher.ticks_mod.history, "window", lambda *a, **k: ([0.0], [1.0]))
    monkeypatch.setattr(watcher.log, "info", lambda *a, **k: None)
    stats(monkeypatch, (0.6, 10, 40.0))
    state = {}
    monkeypatch.setattr(curvevalue, "read", lambda *a, **k: curvevalue.Curve(state["p"], state["tm"] * UNIT, RATE, RATE))
    w, t = me(), target(close_ts=9_200.0)
    sold = None
    for i, tm in enumerate(path):
        state["tm"], state["p"] = tm, (0.3 if tm < 0 else 0.7)
        r = watcher.ProfitWatcher._curve_hit(w, t, 0.40, 9_000.0 + i, 9_000.0 + i)
        if r and r.startswith("curve exit (time to go)"):
            sold = i
            break
    assert sold is not None and sold >= 25 + 30  # the clock restarted when the cushion recovered
