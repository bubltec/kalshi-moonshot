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
