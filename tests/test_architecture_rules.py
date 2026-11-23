"""The architecture rules of CLAUDE.md, held by a test (user, 2026-11-19: "this was already an architectural decision so why it isn't noted anywhere and held to is confusing").

1. Entry runs on the websocket: the entry thread exists, takes quote and index ticks, and a lane it drives has no review or scope timer.
2. No database read on the entry path: a tick on a watched event, and the offer to review it, touch no table.
"""
import inspect
import re
import time
from datetime import UTC, datetime, timedelta

from kalshi_moonshot import entrywatch, restbuys, runner, worker
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


# ---- 2026-11-19, part two: queues and threads on shared memory, under 60 ms a tick, no database or Kalshi read in a decision ----
import threading  # noqa: E402

import pytest  # noqa: E402

from kalshi_moonshot import memguard, steptime  # noqa: E402


@pytest.fixture
def guarded(monkeypatch):
    monkeypatch.setattr(memguard, "raising", True)
    memguard.reset()
    steptime.tick_reset()
    yield
    memguard.reset()
    steptime.tick_reset()


def test_the_guard_itself_fails_a_select_and_a_get_inside_a_decision(guarded, tmp_path):
    s = socket_settings(tmp_path)
    store = Store(s.db_path)
    with memguard.decision("buy"):
        with pytest.raises(memguard.MemoryRuleBroken):
            store.db.execute("SELECT 1").fetchone()
        with pytest.raises(memguard.MemoryRuleBroken):
            memguard.note("rest", "GET /markets/KX-1")
        store.db.execute("CREATE TABLE IF NOT EXISTS scratch (a)")  # a write is not a breach: it is queued or comes after the judgement
    store.db.execute("SELECT 1").fetchone()  # outside a decision: free


def test_a_buy_decision_on_the_socket_path_touches_neither_the_database_nor_kalshi(guarded, tmp_path, monkeypatch):
    """The worker's evaluation, replayed: a paper bet is decided with the bot's memory switches on. Every table read in it would raise."""
    import time as _time

    from kalshi_moonshot import config
    from kalshi_moonshot import store as store_mod
    from tests import decide_support as T

    T.decide(tmp_path, monkeypatch, _time.time(), "")  # cold: tables and the lane's version row are written once
    s = config.load()
    store = Store(s.db_path)
    store.rebuild_book()
    monkeypatch.setitem(store_mod._DEC_BG, "on", True)
    monkeypatch.setitem(store_mod._BACKGROUND, "on", True)
    store.preload_decisions()
    store.bankroll.refresh(object(), s)  # the background job
    store.bankroll.served = True
    from kalshi_moonshot import calibration

    calibration.start_background(lambda: Store(s.db_path), every=3600)  # the record adjustment is answered from memory
    monkeypatch.setattr(calibration, "_SERVED", calibration._SERVED)
    queue_size = store_mod._DEC_Q.qsize()
    real_init = Store.__init__
    made = []
    monkeypatch.setattr(Store, "__init__", lambda self, *a, **k: (made.append(1), real_init(self, *a, **k))[1])
    before = steptime.tick_stats().get("buy evaluate", {}).get("n", 0)
    with steptime.tick("buy evaluate"), memguard.decision("buy"):
        out = decide_with(store, tmp_path, monkeypatch, s)
    assert out.action in ("bet", "pass") and not memguard.breaches
    assert steptime.tick_stats()["buy evaluate"]["n"] == before + 1
    assert steptime.tick_stats()["buy evaluate"]["max"] < steptime.TICK_BUDGET_MS  # one evaluation, memory only, under the budget
    calibration.stop_serving()
    while store_mod._DEC_Q.qsize() > queue_size:  # nothing here runs the writer: drain what the decision queued
        store_mod._DEC_Q.get_nowait()
        store_mod._DEC_Q.task_done()


def decide_with(store, tmp_path, monkeypatch, s):
    from kalshi_moonshot import ticks
    from kalshi_moonshot.strategy import LongShot
    from tests import decide_support as T

    now = __import__("time").time()
    shot = LongShot("KXBTC15M-7", "t", "", 0.80, 0.81, 420.0 / 3600.0, "2099-01-01T00:00:00Z", 420.0 / 3600.0, lane="rs", series="KXBTC15M", side="yes")
    assert ticks is not None
    return runner.evaluate(s, None, None, store, shot, estimators={"market": T.Fixed()})


def test_a_replay_of_ticks_keeps_every_buy_review_and_sell_tick_under_the_budget(guarded, tmp_path, monkeypatch):
    """500 streamed quotes through the sell logic of an open bet and 200 through the entry thread's review of a watched event: no read of the database or Kalshi, and none over 60 ms."""
    from kalshi_moonshot import config, watcher

    w = watcher.ProfitWatcher(lambda: config.load(), None, lambda: None)
    tg = watcher.Target("KXBTC15M-1", "fav", "yes", 0.80, 0.0, 0.0, 0.0, 30.0, 0.10, 0.10, 0.04, 10.0, 0.12, 3.0, 0.80)
    w.targets[("KXBTC15M-1", "fav")] = tg
    for i in range(500):
        bid = 0.80 + ((i % 11) - 5) * 0.004
        w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": bid, "ask": bid + 0.01, "ts": 1000.0 + i})
    stats = steptime.tick_stats()
    assert stats["sell"]["n"] >= 400 and stats["sell"]["max"] < steptime.TICK_BUDGET_MS and stats["sell"]["over"] == 0, stats["sell"]

    ew_settings = socket_settings(tmp_path)
    ew = watcher_for_review(ew_settings)
    for _ in range(200):
        ew._look(("commodity", EVENT))
    stats = steptime.tick_stats()
    assert stats["buy review"]["n"] == 200 and stats["buy review"]["max"] < steptime.TICK_BUDGET_MS, stats["buy review"]


def watcher_for_review(s):
    """An entry watcher whose review reads the stream's copy (memory) with a stored watchlist."""
    from kalshi_moonshot import lanes
    from tests.test_two_level import Client, live_event

    class Memory(Client):
        def event_markets(self, event):
            raise AssertionError("a request")

        def event_markets_memory(self, event):
            return [dict(m) for m in self.markets if m["event_ticker"] == event]

        def event_info_memory(self):
            return {}

    store = Store(s.db_path)
    runner.scope_lane(s, Client(live_event()), store, lanes.spec_for(s, "commodity"))
    w = watcher(s, Memory(live_event()))
    w.evaluate = lambda shot: "seen"
    return w


def test_a_quote_tick_wakes_a_waiting_resting_buy_and_nothing_sleeps_on_a_timer():
    src = inspect.getsource(runner._rest_wait) + inspect.getsource(restbuys.Resting.wait)
    assert "sleep(" not in src
    assert "wait_for" in src  # the wait is on the socket's events
