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
