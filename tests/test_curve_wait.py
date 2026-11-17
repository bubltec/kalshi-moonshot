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


# The loss-side sales without the margin sale (2026-11-17, user: "lets. fix the curve exit, because WTI just landed 2 0s"; the margin sale had been turned off on the commodity lanes because it sold 25 winners of 27, and the time-to-go sale hung on it).
def test_time_to_go_sells_a_bet_behind_its_target_with_the_margin_sale_off(monkeypatch):
    t = target(curve_margin=-1.0)  # no margin sale on the lane; time-to-go on (curve_behind_last = 240)
    assert run(monkeypatch, t, [-1.6] * 5, bid=0.45, p=0.30, rate=None) == (None, None)  # 900 s left: not in the last four minutes yet
    t2 = target(curve_margin=-1.0)
    monkeypatch.setattr(watcher.ticks_mod.history, "window", lambda *a, **k: ([0.0], [1.0]))
    monkeypatch.setattr(watcher.log, "info", lambda *a, **k: None)
    stats(monkeypatch, None)
    monkeypatch.setattr(curvevalue, "read", lambda *a, **k: curvevalue.Curve(0.30, -1.6 * UNIT, RATE, RATE))
    reason = watcher.ProfitWatcher._curve_hit(me(), t2, 0.45, CLOSE - 200.0, CLOSE - 200.0)  # 200 s left, the curve's chance 0.30, deep under the target: no wait
    assert reason is not None and reason.startswith("curve exit (time to go)")


def test_with_the_margin_sale_off_a_bid_far_over_the_curves_chance_is_not_sold(monkeypatch):
    """The sale that sold 25 winners of 27 on the commodity lanes: bid 0.89 against a curve chance of 0.67."""
    t = target(curve_margin=-1.0)
    monkeypatch.setattr(watcher.ticks_mod.history, "window", lambda *a, **k: ([0.0], [1.0]))
    monkeypatch.setattr(watcher.log, "info", lambda *a, **k: None)
    stats(monkeypatch, (0.6, 10, 5.0))
    monkeypatch.setattr(curvevalue, "read", lambda *a, **k: curvevalue.Curve(0.67, 0.4 * UNIT, RATE, RATE))
    w = me()
    for k in range(120):  # two minutes with the bid 0.21 over the curve's chance, inside the last four minutes
        now = CLOSE - 200.0 + k
        assert watcher.ProfitWatcher._curve_hit(w, t, 0.89, now, now) is None
    t_on = target(curve_margin=0.06)
    sold = [watcher.ProfitWatcher._curve_hit(w, t_on, 0.89, CLOSE - 200.0 + k, CLOSE - 200.0 + k) for k in range(120)]
    assert any(s and s.startswith("curve exit: bid") for s in sold)  # with the margin sale on, the same readings sell


def test_with_nothing_set_the_curve_exit_reads_nothing(monkeypatch):
    t = target(curve_margin=-1.0, curve_behind_last=-1.0)
    called = []
    monkeypatch.setattr(watcher.ticks_mod.history, "window", lambda *a, **k: called.append(1) or ([0.0], [1.0]))
    assert watcher.ProfitWatcher._curve_hit(me(), t, 0.45, CLOSE - 200.0, CLOSE - 200.0) is None and called == []


def test_a_lane_with_only_time_to_go_set_gets_a_watched_position(tmp_path, monkeypatch):
    from kalshi_moonshot import config, lanes

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    base = "[lane.a]\nestimator='ladder'\nprice=[0.1,0.9]\nhours=[0.0,1.0]\n"
    (tmp_path / "strategies.toml").write_text(base)
    assert not lanes.takes_profit(lanes.lane_specs(config.load())["a"])
    (tmp_path / "strategies.toml").write_text(base + "curve_behind_last=240\n")
    assert lanes.takes_profit(lanes.lane_specs(config.load())["a"])
