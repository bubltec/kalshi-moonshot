import time
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_moonshot import entrywatch, lanes, runner
from kalshi_moonshot.estimators import build_estimators
from kalshi_moonshot.store import Store
from tests.test_two_level import Client, live_event, live_quant, settings

EVENT = "KXGOLDD-26OCT19"


def socket_settings(tmp_path):
    (tmp_path / "strategies.toml").write_text('[lane.commodity]\nentry_socket = true\n')
    return settings(tmp_path)


def quote(bid, ask, ticker=f"{EVENT}-T3970"):
    return {"kind": "quote", "ticker": ticker, "bid": bid, "ask": ask, "ts": time.time()}


def watcher(s, client):
    store_path = s.db_path
    return entrywatch.EntryWatcher(
        lambda: s, client, lambda: Store(store_path), lambda store: build_estimators(None, None, None, live_quant()),
    )


def test_entry_socket_needs_a_two_level_lane(tmp_path):
    (tmp_path / "strategies.toml").write_text('[lane.x]\nestimator = "llm"\nprice = [0.1, 0.2]\nhours = [1, 2]\nseries = ["A"]\nentry_socket = true\n')
    with pytest.raises(ValueError, match="entry_socket needs review_minutes"):
        lanes.lane_specs(settings(tmp_path))


def test_it_is_off_unless_a_lane_asks(tmp_path):
    assert not entrywatch.wanted(settings(tmp_path))
    assert entrywatch.wanted(socket_settings(tmp_path))


def test_the_band_check_looks_at_either_side_the_lane_may_buy():
    spec = lanes.LaneSpec("x", "ladder", "x", (0.70, 0.97), (0, 1), 0.05, 0.0, sides=("yes", "no"))
    assert entrywatch.in_band(spec, 0.84, 0.86)  # yes ask 0.86
    assert entrywatch.in_band(spec, 0.10, 0.12)  # no ask 0.90
    assert not entrywatch.in_band(spec, 0.40, 0.45)  # both sides near a coin flip
    assert not entrywatch.in_band(lanes.LaneSpec("x", "ladder", "x", (0.70, 0.97), (0, 1), 0.05, 0.0), 0.10, 0.12)  # yes only


def test_the_entry_window_follows_minutes_elapsed():
    spec = lanes.LaneSpec("x", "ladder", "x", (0.0, 1.0), (0, 1), 0.05, 0.0, timed_suffix="15M", timed_minutes=15.0, entry_elapsed=(4.0, 12.0))
    now = datetime.now(UTC)
    iso = lambda minutes_left: (now + timedelta(minutes=minutes_left)).isoformat()  # noqa: E731
    assert not entrywatch.in_window(spec, iso(13), now)  # 2 minutes in
    assert entrywatch.in_window(spec, iso(8), now)  # 7 minutes in
    assert not entrywatch.in_window(spec, iso(1), now)  # 14 minutes in
    assert not entrywatch.in_window(spec, iso(-1), now)  # closed


def test_a_quote_in_range_queues_one_review_and_a_repeat_does_not(tmp_path):
    s = socket_settings(tmp_path)
    w = watcher(s, Client(live_event()))
    w.watch = {("commodity", EVENT): {"close": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}}
    w.on_event(quote(0.85, 0.87))
    w.on_event(quote(0.86, 0.88))
    assert w._jobs.qsize() == 1
    w.on_event(quote(0.40, 0.45, ticker="KXOTHER-1-T1"))  # not watched
    assert w._jobs.qsize() == 1


def test_a_quote_whose_ticker_is_the_event_queues_the_same_review(tmp_path):
    s = socket_settings(tmp_path)
    w = watcher(s, Client(live_event()))
    event = "KXGOLD15M-26OCT271515"  # Polymarket's ticker is the event; Kalshi adds a strike suffix
    w.watch = {("commodity", event): {"close": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}}
    w.on_event(quote(0.85, 0.87, ticker=event))
    assert w._jobs.qsize() == 1
    assert w._jobs.get_nowait() == ("commodity", event)


def test_a_review_uses_the_lanes_own_venue(tmp_path):
    s = socket_settings(tmp_path)
    store = Store(s.db_path)
    kalshi, poly = Client(live_event()), Client(live_event())
    runner.scope_lane(s, kalshi, store, lanes.spec_for(s, "commodity"))
    kalshi.calls.clear()
    w = watcher(s, kalshi)
    w.client_for = lambda lane: poly
    assert w.review("commodity", EVENT) == 1
    assert kalshi.calls == []
    assert poly.calls == [f"event:{EVENT}"]


def test_a_quote_out_of_range_queues_nothing(tmp_path):
    s = socket_settings(tmp_path)
    w = watcher(s, Client(live_event()))
    w.watch = {("commodity", EVENT): {"close": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}}
    w.on_event(quote(0.40, 0.45))
    assert w._jobs.qsize() == 0


def test_a_lane_without_entry_socket_is_ignored(tmp_path):
    s = settings(tmp_path)
    w = watcher(s, Client(live_event()))
    w.watch = {("commodity", EVENT): {"close": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}}
    w.on_event(quote(0.85, 0.87))
    assert w._jobs.qsize() == 0


def test_a_review_reads_only_that_event_and_queues_the_same_evaluations(tmp_path):
    s = socket_settings(tmp_path)
    store = Store(s.db_path)
    client = Client(live_event())
    runner.scope_lane(s, client, store, lanes.spec_for(s, "commodity"))
    client.calls.clear()
    w = watcher(s, client)
    assert w.review("commodity", EVENT) == 1
    assert client.calls == [f"event:{EVENT}"]
    row = Store(s.db_path).db.execute("SELECT kind, priority FROM jobs WHERE kind='evaluate'").fetchone()
    assert row["priority"] == lanes.spec_for(s, "commodity").priority
    assert w.review("commodity", EVENT) == 0  # the evaluation is already queued: nothing doubled


def test_refresh_loads_the_watchlists_of_socket_lanes_only(tmp_path):
    s = socket_settings(tmp_path)
    Store(s.db_path).set_watch("commodity", {EVENT: {"close": "2099-01-01T00:00:00Z"}})
    w = watcher(s, Client([]))
    w.refresh()
    assert list(w.watch) == [("commodity", EVENT)]


def _approach_watcher():
    w = entrywatch.EntryWatcher(lambda: None, None, lambda: None, lambda store: {})  # type: ignore[arg-type]
    spec = lanes.LaneSpec("x", "ladder", "x", (0.76, 0.92), (0, 1), 0.05, 0.0, sides=("yes", "no"), entry_approach=0.10, entry_arm_seconds=30.0)
    return w, spec


def _feed(w, ts, bid, ask, ticker="KXBTC15M-1"):
    row = {"kind": "quote", "ticker": ticker, "bid": bid, "ask": ask, "ts": ts}
    w._hist.setdefault(ticker, entrywatch.deque(maxlen=400)).append((ts, bid, ask))
    return row


def test_a_side_gaining_toward_the_floor_is_armed_and_fires_when_it_gets_there():
    from kalshi_moonshot.strategy import LongShot

    w, spec = _approach_watcher()
    now = datetime.now(UTC)
    _feed(w, 100.0, 0.66, 0.68)
    e = _feed(w, 110.0, 0.72, 0.74)  # up 6c over 10 s, 2c under the floor... 0.74 is within 10c under 0.76
    assert w._approach(e, "x", spec, {}, now) is True  # armed, so a taker buy is not also queued
    key = ("x", "KXBTC15M-1", "yes")
    assert key in w._armed and w._arm_jobs.get_nowait() == ("warm", key)
    w._armed[key]["shot"] = LongShot("KXBTC15M-1", "t", "", 0.72, 0.74, 10.0, "2099-01-01T00:00:00Z", 0.1, lane="x", series="KXBTC15M", side="yes")
    e = _feed(w, 115.0, 0.75, 0.76)  # the ask is the floor: a bid there would take, so it waits
    assert w._approach(e, "x", spec, {}, now) is True and w._arm_jobs.empty()
    e = _feed(w, 120.0, 0.76, 0.78)  # the ask is above the floor: the bid goes out at the floor
    assert w._approach(e, "x", spec, {}, now) is True
    job = w._arm_jobs.get_nowait()
    assert job[0] == "fire" and job[2].ask == pytest.approx(0.76) and key in w._armed


def test_a_side_that_is_not_gaining_is_not_armed_and_an_old_arming_is_reset():
    w, spec = _approach_watcher()
    now = datetime.now(UTC)
    _feed(w, 100.0, 0.74, 0.76)
    e = _feed(w, 110.0, 0.70, 0.72)  # falling
    assert w._approach(e, "x", spec, {}, now) is False and not w._armed
    _feed(w, 120.0, 0.60, 0.62)
    e = _feed(w, 130.0, 0.70, 0.72)  # gaining now: armed
    w._approach(e, "x", spec, {}, now)
    key = ("x", "KXBTC15M-1", "yes")
    assert key in w._armed
    e = _feed(w, 170.0, 0.60, 0.62)  # 40 s later it has not reached the floor: dropped; not gaining over the last 25 s so it stays dropped
    w._approach(e, "x", spec, {}, now)
    assert key not in w._armed


def full_lane_settings(tmp_path, cap):
    (tmp_path / "strategies.toml").write_text(f'[lane.commodity]\nentry_socket = true\nmax_open = {cap}\n')
    return settings(tmp_path)


def hold(store, ticker):
    store.add_decision(ticker=ticker, title="t", bid=0.8, ask=0.82, model_p=0.9, edge=0.05, action="bet", mode="observe", lane="commodity", side="yes")


def test_a_review_of_a_full_lane_reads_nothing(tmp_path):
    s = full_lane_settings(tmp_path, 1)
    store = Store(s.db_path)
    client = Client(live_event())
    runner.scope_lane(s, client, store, lanes.spec_for(s, "commodity"))
    hold(store, "KXGOLDD-26OCT20-T4000")  # one bet open in another event: the lane holds its one
    client.calls.clear()
    w = watcher(s, client)
    assert w.review("commodity", EVENT) == 0
    assert client.calls == []  # no request spent on a market the lane cannot take
    assert Store(s.db_path).db.execute("SELECT COUNT(*) FROM jobs WHERE kind='evaluate'").fetchone()[0] == 0


def test_a_review_goes_ahead_while_the_lane_has_room_or_the_bet_is_this_event(tmp_path):
    s = full_lane_settings(tmp_path, 2)
    store = Store(s.db_path)
    client = Client(live_event())
    runner.scope_lane(s, client, store, lanes.spec_for(s, "commodity"))
    hold(store, "KXGOLDD-26OCT20-T4000")  # one of two slots used
    w = watcher(s, client)
    assert w.review("commodity", EVENT) == 1
    full = entrywatch.lane_full
    spec = lanes.spec_for(s, "commodity")
    assert not full(store, spec, {EVENT})
    hold(store, f"{EVENT}-T3900")  # the second slot, taken in this very event
    assert not full(store, spec, {EVENT})  # the other bet numbers one of two: this event may still be reviewed
    assert not full(store, spec, {"KXGOLDD-26OCT20"})  # and so may the other event it holds
    assert full(store, spec, {"KXGOLDD-26OCT22"})  # a third event finds both slots taken


def test_arming_a_side_in_a_full_lane_prepares_no_shot(tmp_path):
    s = full_lane_settings(tmp_path, 1)
    store = Store(s.db_path)
    client = Client(live_event())
    runner.scope_lane(s, client, store, lanes.spec_for(s, "commodity"))
    hold(store, "KXGOLDD-26OCT20-T4000")
    client.calls.clear()
    w = watcher(s, client)
    key = ("commodity", f"{EVENT}-T3970", "yes")
    w._armed[key] = {"t": time.time(), "shot": None, "offered": 0.0}
    w._warm(key)
    assert client.calls == [] and w._armed[key]["shot"] is None


def _move_watcher(**kw):
    w = entrywatch.EntryWatcher(lambda: None, None, lambda: None, lambda store: {})  # type: ignore[arg-type]
    spec = lanes.LaneSpec("x", "ladder", "x", (0.03, 0.70), (0, 1), 0.06, 0.0, sides=("yes", "no"), entry_move=0.40, entry_move_seconds=3.0, entry_arm_seconds=10.0, **kw)
    return w, spec


def test_a_40c_fall_in_3s_queues_a_buy_of_yes_and_a_40c_rise_a_buy_of_no():
    now = datetime.now(UTC)
    w, spec = _move_watcher()
    _feed(w, 100.0, 0.84, 0.86)
    w._move(_feed(w, 101.0, 0.60, 0.62), "x", spec, {}, now)  # 24c: not enough
    assert w._arm_jobs.empty()
    w._move(_feed(w, 102.5, 0.44, 0.46), "x", spec, {}, now)  # 40c under the 0.85 mid of 2.5 s ago
    job = w._arm_jobs.get_nowait()
    assert job[0] == "move" and job[1] == ("x", "KXBTC15M-1", "yes") and job[3] == pytest.approx(0.85) and job[4] == pytest.approx(0.45)
    w2, spec = _move_watcher()
    _feed(w2, 100.0, 0.14, 0.16)
    w2._move(_feed(w2, 102.0, 0.54, 0.56), "x", spec, {}, now)  # YES up 40c: NO fell, so buy NO
    assert w2._arm_jobs.get_nowait()[1][2] == "no"


def test_a_slow_move_or_a_repeat_queues_nothing():
    now = datetime.now(UTC)
    w, spec = _move_watcher()
    _feed(w, 100.0, 0.84, 0.86)
    w._move(_feed(w, 104.0, 0.44, 0.46), "x", spec, {}, now)  # 40c, but over 4 s: the 3 s span only sees the end
    assert w._arm_jobs.empty()
    w._move(_feed(w, 105.0, 0.20, 0.22), "x", spec, {}, now)  # 24c from the 0.45 mid a second ago; the 0.85 is 5 s back, outside the span
    assert w._arm_jobs.empty()
    w2, spec = _move_watcher()
    _feed(w2, 100.0, 0.84, 0.86)
    w2._move(_feed(w2, 101.0, 0.44, 0.46), "x", spec, {}, now)
    assert not w2._arm_jobs.empty()
    w2._arm_jobs.get_nowait()
    w2._move(_feed(w2, 102.0, 0.30, 0.32), "x", spec, {}, now)  # still falling: one buy per rest window
    assert w2._arm_jobs.empty()


def test_a_move_lane_never_takes_the_ask_and_a_side_it_does_not_trade_is_skipped():
    now = datetime.now(UTC)
    w, spec = _move_watcher()
    spec = lanes.LaneSpec(**{**spec.__dict__, "sides": ("no",)})
    _feed(w, 100.0, 0.84, 0.86)
    w._move(_feed(w, 101.0, 0.44, 0.46), "x", spec, {}, now)  # YES fell: that would be a YES buy
    assert w._arm_jobs.empty()


def test_move_settings_do_not_change_other_lanes_versions():
    plain = lanes.LaneSpec("x", "ladder", "x", (0.1, 0.9), (0, 1), 0.05, 0.0)
    assert "entry_move" not in lanes.settings_of(plain, config_stub()) and "scalp_plateau_seconds" not in lanes.settings_of(plain, config_stub())
    moved = lanes.LaneSpec("x", "ladder", "x", (0.1, 0.9), (0, 1), 0.05, 0.0, entry_move=0.4, scalp_plateau_seconds=10)
    out = lanes.settings_of(moved, config_stub())
    assert out["entry_move"] == "0.4" and out["scalp_plateau_seconds"] == "10"


def config_stub():
    from kalshi_moonshot import config
    return config.load()


CLOSE = 100_000.0


def _late_watcher(path, strike=100.0, agree=4):
    """A watcher that has seen one index tick a second from the window's open to minute 12, following `path(t)` (seconds before the close -> index value)."""
    w = entrywatch.EntryWatcher(lambda: None, None, lambda: None, lambda store: {})  # type: ignore[arg-type]
    spec = lanes.LaneSpec("x", "ladder", "x", (0.5, 0.99), (0, 0.1), 0.10, 0.0, sides=("yes", "no"), late_steps=5, late_agree=agree, late_min_z=0.8)
    ring = w._idx.setdefault("BRTI", entrywatch.deque(maxlen=2400))
    for sec in range(900, 180, -1):
        ring.append((CLOSE - sec, path(sec)))
    info = {"close": datetime.fromtimestamp(CLOSE, UTC).isoformat(), "ticker": "KXBTC15M-X", "strike": strike, "series": "KXBTC15M"}
    return w, spec, info


def _wiggle(sec):
    return 0.002 * ((sec * 7919) % 13 - 6)  # a small, repeatable jitter so the coin has a usual move


def _tick(w, spec, info, sec_left, value):
    w._idx["BRTI"].append((CLOSE - sec_left, value))
    w._late("x", spec, info, "BRTI", CLOSE - sec_left, value)


def test_a_steady_climb_over_the_strike_buys_yes_at_the_first_new_high():
    w, spec, info = _late_watcher(lambda sec: 100.0 + max(0, 700 - sec) * 0.004 + _wiggle(sec) if sec > 360 else 100.0 + (360 - sec) * 0.012 + 2.8 + _wiggle(sec))
    top = max(x for _, x in w._idx["BRTI"])
    _tick(w, spec, info, 170, top + 0.5)  # a new high of the whole window, well over the strike
    job = w._arm_jobs.get_nowait()
    assert job[0] == "late" and job[1] == ("x", "KXBTC15M-X", "yes") and job[2]["trend"] == "up" and job[2]["z"] >= 0.8
    _tick(w, spec, info, 160, top + 1.0)  # one look per market
    assert w._arm_jobs.empty()


def test_a_falling_history_buys_no_when_the_index_sets_a_new_low_under_the_strike():
    w, spec, info = _late_watcher(lambda sec: 100.0 - max(0, 700 - sec) * 0.004 + _wiggle(sec) if sec > 360 else 100.0 - (360 - sec) * 0.012 - 2.8 + _wiggle(sec), strike=100.0)
    low = min(x for _, x in w._idx["BRTI"])
    _tick(w, spec, info, 170, low - 0.5)
    assert w._arm_jobs.get_nowait()[1][2] == "no"


def test_a_choppy_history_is_passed_and_so_is_a_new_extreme_too_close_to_the_strike():
    w, spec, info = _late_watcher(lambda sec: 100.0 + (0.05 if (sec // 36) % 2 else -0.05) + _wiggle(sec))
    _tick(w, spec, info, 170, 101.0)
    assert w._arm_jobs.empty() and ("x", "KXBTC15M-X") in w._late_done  # a 3-2 or worse vote is mixed: no buy
    climb = lambda sec: 100.0 + max(0, 700 - sec) * 0.004 + _wiggle(sec) if sec > 360 else 100.0 + (360 - sec) * 0.012 + 2.8 + _wiggle(sec)  # noqa: E731
    w2, spec, info = _late_watcher(climb)
    top = max(x for _, x in w2._idx["BRTI"])
    info["strike"] = top  # the new high is only a hair over the strike: far under 0.8 of a usual move
    _tick(w2, spec, info, 170, top + 0.0015)
    assert w2._arm_jobs.empty() and ("x", "KXBTC15M-X") in w2._late_done


def test_a_late_lane_ignores_ticks_outside_minutes_12_to_14():
    w, spec, info = _late_watcher(lambda sec: 100.0 + (900 - sec) * 0.01 + _wiggle(sec))
    _tick(w, spec, info, 40, 120.0)  # 14.3 minutes in: too late
    assert w._arm_jobs.empty() and not w._late_done


def test_prune_forgets_markets_that_are_over_and_keeps_live_ones():
    w, _spec, _info = _late_watcher(lambda sec: 100.0)
    now = CLOSE + 1000.0
    w._hist["OLD"] = entrywatch.deque([(now - 500.0, 0.4, 0.5)], maxlen=400)
    w._hist["LIVE"] = entrywatch.deque([(now - 5.0, 0.4, 0.5)], maxlen=400)
    w._late_done[("x", "OLD")] = CLOSE  # closed 1000 s ago
    w._late_done[("x", "LIVE")] = now + 100.0
    w._late_state[("x", "OLD")] = {"close": CLOSE}
    w._late_state[("x", "LIVE")] = {"close": now + 100.0}
    w._moved[("x", "OLD")] = now - 4000.0
    w._moved[("x", "LIVE")] = now - 10.0
    w._armed[("x", "OLD", "yes")] = {"t": now - 4000.0, "shot": None, "offered": 0.0}
    w.prune(now)
    assert list(w._hist) == ["LIVE"] and list(w._late_done) == [("x", "LIVE")] and list(w._late_state) == [("x", "LIVE")]
    assert list(w._moved) == [("x", "LIVE")] and not w._armed
    assert len(w._idx["BRTI"]) > 0  # the index rings are bounded by their length, not pruned


def test_the_refresh_thread_prunes_every_pass():
    w = entrywatch.EntryWatcher(lambda: None, None, lambda: None, lambda store: {})  # type: ignore[arg-type]
    w._hist["GONE"] = entrywatch.deque([(1.0, 0.4, 0.5)], maxlen=400)
    w.refresh = lambda: w._stop.set()  # type: ignore[method-assign]
    w._run_refresh()
    assert "GONE" not in w._hist


def test_a_review_reads_the_listing_through_the_shared_cache(tmp_path):
    """2026-11-07: entry-review was 55-88 uncached GET /markets a minute. A review runs inside client.shared(max_age) so a listing young enough comes from memory."""
    from contextlib import contextmanager

    s = socket_settings(tmp_path)
    store = Store(s.db_path)
    client = Client(live_event())
    runner.scope_lane(s, client, store, lanes.spec_for(s, "commodity"))
    seen = []

    @contextmanager
    def shared(max_age):
        seen.append(("enter", max_age))
        try:
            yield
        finally:
            seen.append(("exit", max_age))

    client.shared = shared
    entrywatch.EntryWatcher(lambda: s, client, lambda: Store(s.db_path), lambda store: build_estimators(None, None, None, live_quant())).review("commodity", EVENT)
    if s.discovery_enabled and s.discovery_max_age > 0:
        assert seen == [("enter", s.discovery_max_age), ("exit", s.discovery_max_age)]
