"""The architecture rules of CLAUDE.md, held by a test (user, 2026-11-19: "this was already an architectural decision so why it isn't noted anywhere and held to is confusing").

1. Entry runs on the websocket: the entry thread exists, takes quote and index ticks, and a lane it drives has no review or scope timer.
2. No database read on the entry path: a tick on a watched event, and the offer to review it, touch no table.
"""
import inspect
import re
import time
from datetime import UTC, datetime, timedelta

from kalshi_moonshot import entrywatch, worker
from kalshi_moonshot.store import Store
from tests.test_entrywatch import EVENT, socket_settings, watcher
from tests.test_two_level import Client, live_event


def test_the_entry_thread_has_no_timer_and_no_table_read_in_its_socket_path():
    src = inspect.getsource(entrywatch.EntryWatcher)
    for name in ("on_event", "_index_review", "_offer", "_watched", "_prune_maybe"):
        body = inspect.getsource(getattr(entrywatch.EntryWatcher, name))
        assert not re.search(r"\.db\b|execute\(|SELECT|store_factory|time\.sleep|\.wait\(", body), f"{name} reads the database or waits on a timer"
    assert "_run_refresh" not in src and "REFRESH_EVERY" not in inspect.getsource(entrywatch)  # no refresh timer thread


def test_a_tick_on_a_watched_event_reads_no_table(tmp_path):
    s = socket_settings(tmp_path)
    w = watcher(s, Client(live_event()))
    w.watch = {("commodity", EVENT): {"close": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}}
    reads = []
    real = Store.__init__

    def spy(self, *a, **k):
        reads.append(1)
        return real(self, *a, **k)

    Store.__init__ = spy
    try:
        w.on_event({"kind": "quote", "ticker": f"{EVENT}-T3970", "bid": 0.85, "ask": 0.87, "ts": time.time()})
    finally:
        Store.__init__ = real
    assert w._jobs.qsize() == 1 and not reads  # queued the review, opened no store


def test_the_worker_has_no_review_or_scope_timer_for_a_driven_lane():
    src = inspect.getsource(worker.Worker.do_lane_review) + inspect.getsource(worker.Worker.do_lane_scope)
    assert src.count("if not self._driven(spec)") == 2
    assert "WATCHDOG" not in inspect.getsource(worker)
