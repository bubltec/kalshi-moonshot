"""A pass's decision row is written off the evaluation's thread (2026-11-17, user: "why are you waiting on a database?"; "you shouldn't be using the db. everything in current scope for a buy/sell judgement comes from memory")."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import test_risk_score as T  # noqa: E402

from kalshi_moonshot import config  # noqa: E402
from kalshi_moonshot import store as store_mod  # noqa: E402
from kalshi_moonshot.store import Store  # noqa: E402


@pytest.fixture(autouse=True)
def clean():
    yield
    store_mod.flush_decisions(2.0)
    store_mod.queue_decisions(False)
    store_mod._LAST_DEC.clear()


class Watch:
    """Every statement and commit a store issues from here on."""

    def __init__(self, store):
        self.seen, self.inner = [], store.db
        store.db = self

    def execute(self, sql, *args):
        self.seen.append(sql)
        return self.inner.execute(sql, *args)

    def commit(self):
        self.seen.append("COMMIT")
        return self.inner.commit()

    def __getattr__(self, name):
        return getattr(self.inner, name)


def mk(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    return s, Store(s.db_path)


def rows(s):
    return Store(s.db_path).db.execute("SELECT ticker, action, lane, ts, reason FROM decisions ORDER BY ts").fetchall()


def test_unit_off_a_row_is_written_at_once_as_it_always_was(tmp_path, monkeypatch):
    s, store = mk(tmp_path, monkeypatch)
    w = Watch(store)
    store.add_decision(ticker="A", action="pass", mode="observe", lane="x")
    assert any("INSERT INTO decisions" in q for q in w.seen) and "COMMIT" in w.seen and len(rows(s)) == 1


def test_unit_on_a_pass_issues_no_statement_and_is_written_by_the_writer_with_its_own_time(tmp_path, monkeypatch):
    s, store = mk(tmp_path, monkeypatch)
    store_mod.queue_decisions()
    w = Watch(store)
    t0 = time.time()
    for k in range(25):
        store.add_decision(ticker=f"A{k}", action="pass", mode="observe", lane="x", reason=f"r{k}")
    assert w.seen == []  # the caller's connection was never touched
    assert store_mod.flush_decisions(5.0)
    got = rows(s)
    assert [r["ticker"] for r in got] == [f"A{k}" for k in range(25)] and got[3]["reason"] == "r3"
    assert all(t0 - 1 <= r["ts"] <= t0 + 2 for r in got)  # the time is the decision's, not the write's


def test_unit_on_a_bet_is_still_written_at_once(tmp_path, monkeypatch):
    s, store = mk(tmp_path, monkeypatch)
    store_mod.queue_decisions()
    w = Watch(store)
    rid = store.add_decision(ticker="B", action="bet", mode="trade", lane="x", ask=0.5, side="yes", stake_usd=1.0, contracts=2.0)
    assert rid > 0 and any("INSERT INTO decisions" in q for q in w.seen) and "COMMIT" in w.seen
    assert [r["action"] for r in rows(s)] == ["bet"]  # there before any flush: the watcher and the books read it now


def test_unit_the_review_cooldown_is_answered_from_memory(tmp_path, monkeypatch):
    s, store = mk(tmp_path, monkeypatch)
    store_mod.queue_decisions()
    w = Watch(store)
    now = time.time()
    monkeypatch.setattr(store_mod, "_PROC_START", now - 3600.0)  # a bot that has been up an hour
    store.add_decision(ticker="C", action="pass", mode="observe", lane="x")
    assert store.decided_since("C", now - 60, "x") is True  # the row may still be in the queue: memory knows
    assert store.decided_since("C", now - 60) is True
    assert store.decided_since("C", now - 60, "other") is False  # this lane has logged nothing on it since the bot started
    assert store.decided_since("D", now - 60, "x") is False
    assert w.seen == []  # none of the four asked the database
    store_mod.flush_decisions(5.0)
    w.seen.clear()
    assert store.decided_since("C", store_mod._PROC_START - 3600.0, "other") is False
    assert any("FROM decisions" in q for q in w.seen)  # from before the process began memory cannot know: the table is asked


def test_unit_a_failed_write_is_retried_and_the_rows_are_not_lost(tmp_path, monkeypatch):
    s, store = mk(tmp_path, monkeypatch)
    monkeypatch.setattr(store_mod.time, "sleep", lambda *_: None)
    real = Store.__init__
    fails = {"n": 1}

    def flaky(self, *a, **k):
        if fails["n"] > 0:
            fails["n"] -= 1
            raise RuntimeError("database not reachable")
        real(self, *a, **k)

    store_mod.queue_decisions()
    monkeypatch.setattr(Store, "__init__", flaky)
    store.add_decision(ticker="E", action="pass", mode="observe", lane="x")
    deadline = time.monotonic() + 5.0
    while store_mod._DEC_Q.unfinished_tasks and time.monotonic() < deadline:
        pass
    monkeypatch.setattr(Store, "__init__", real)
    assert [r["ticker"] for r in rows(s)] == ["E"]


def test_integration_an_evaluation_that_passes_ends_without_a_write(tmp_path, monkeypatch):
    now = time.time()
    seen = []
    real = Store.__init__

    def spy(self, *a, **k):
        real(self, *a, **k)
        inner = self.db

        class W:
            def execute(_, sql, *args):
                seen.append(sql)
                return inner.execute(sql, *args)

            def commit(_):
                seen.append("COMMIT")
                return inner.commit()

            def __getattr__(_, name):
                return getattr(inner, name)

        self.db = W()

    out = T.decide(tmp_path, monkeypatch, now, "buy_chain=['risk_score']\nrisk_score_buy=0.01")  # once with the writer off: the lane's version row and the like are written
    assert out.action == "pass"
    store_mod.queue_decisions()
    monkeypatch.setattr(Store, "__init__", spy)
    main_thread_seen_before = len(seen)
    out = T.decide(tmp_path, monkeypatch, now, "buy_chain=['risk_score']\nrisk_score_buy=0.01")
    assert out.action == "pass"
    mine = [q for q in seen[main_thread_seen_before:] if "INSERT INTO decisions" in q]
    assert store_mod.flush_decisions(5.0)
    s = config.load()
    got = Store(s.db_path).db.execute("SELECT action, reason FROM decisions WHERE lane='rs' ORDER BY id").fetchall()
    assert len(got) == 2 and got[1]["action"] == "pass" and "chain risk_score" in got[1]["reason"]  # the row is there, written by the writer
    assert len(mine) <= 1  # at most the writer's own insert was seen through the spied stores; the evaluation's store issued none


def test_integration_with_the_bots_switches_on_an_evaluation_sends_the_database_nothing(tmp_path, monkeypatch):
    """The whole judgement from memory: the pass row is queued, the bet book and the record are in memory. Lane settings that make the evaluation ask about open bets, the event's other bets and the lane's last sale."""
    import threading

    from kalshi_moonshot import calibration

    now = time.time()
    extra = "buy_chain=['risk_score']\nrisk_score_buy=0.01\nreentry_cooldown_seconds=900\nmax_open=5\nentry_scenario_view=true"
    T.decide(tmp_path, monkeypatch, now, extra)  # cold: the tables and the lane's version row are written once
    s = config.load()
    store_mod.queue_decisions()  # what worker.start turns on
    Store(s.db_path).rebuild_book()
    store_mod.serve_from_memory()
    calibration.start_background(lambda: Store(s.db_path), every=3600)
    T.decide(tmp_path, monkeypatch, now, extra)  # warm
    seen, me, real = [], threading.get_ident(), Store.__init__

    def spy(self, *a, **k):
        real(self, *a, **k)
        inner = self.db

        class W:
            def execute(_, sql, *args):
                if threading.get_ident() == me:
                    seen.append(sql)
                return inner.execute(sql, *args)

            def commit(_):
                if threading.get_ident() == me:
                    seen.append("COMMIT")
                return inner.commit()

            def __getattr__(_, name):
                return getattr(inner, name)

        self.db = W()

    monkeypatch.setattr(Store, "__init__", spy)
    out = T.decide(tmp_path, monkeypatch, now, extra)
    assert out.action == "pass"
    mine = [q for q in seen if "INTO windows" not in q and q != "COMMIT"]  # the helper seeds the market's window row itself; nothing else may come from this thread
    assert mine == [], mine
    assert store_mod.flush_decisions(5.0)
    assert Store(s.db_path).db.execute("SELECT count(*) AS n FROM decisions WHERE lane='rs' AND action='pass'").fetchone()["n"] == 3  # and all three rows were recorded
    calibration.stop_serving()


def test_unit_the_entry_snapshot_is_taken_by_the_writer_as_of_the_decisions_time_and_merged(tmp_path, monkeypatch):
    s, store = mk(tmp_path, monkeypatch)
    store_mod.queue_decisions()
    asked = []

    def job(ts):
        asked.append(ts)
        return '{"spread": 0.02, "elapsed_min": 4.0}'

    w = Watch(store)
    store.add_decision(ticker="S1", action="pass", mode="observe", lane="x", ts=1234.5, _factors_job=job)
    store.add_decision(ticker="S2", action="pass", mode="observe", lane="x", ts=1240.0, factors='{"risk": {"ask": 0.6}, "spread": 0.01}', _factors_job=job)
    store.add_decision(ticker="S3", action="pass", mode="observe", lane="x", ts=1250.0, _factors_job=lambda ts: (_ for _ in ()).throw(RuntimeError("tape down")))
    assert w.seen == [] and asked == []  # the evaluation's thread took no snapshot and wrote nothing
    assert store_mod.flush_decisions(5.0)
    import json

    got = {r["ticker"]: r["factors"] for r in Store(s.db_path).db.execute("SELECT ticker, factors FROM decisions").fetchall()}
    assert asked == [1234.5, 1240.0]  # as of each decision's own time
    assert json.loads(got["S1"]) == {"spread": 0.02, "elapsed_min": 4.0}
    assert json.loads(got["S2"]) == {"spread": 0.01, "elapsed_min": 4.0, "risk": {"ask": 0.6}}  # what the row already carried wins
    assert "S3" in got and got["S3"] is None  # a snapshot that fails never loses the row


def test_unit_a_bets_snapshot_is_taken_when_its_row_is_written(tmp_path, monkeypatch):
    s, store = mk(tmp_path, monkeypatch)
    store_mod.queue_decisions()
    asked = []
    store.add_decision(ticker="B1", action="bet", mode="trade", lane="x", ask=0.5, side="yes", stake_usd=1.0, contracts=2.0, ts=99.0, _factors_job=lambda ts: asked.append(ts) or '{"spread": 0.03}')
    row = Store(s.db_path).db.execute("SELECT factors FROM decisions WHERE ticker='B1'").fetchone()
    assert asked == [99.0] and row["factors"] == '{"spread": 0.03}'


def test_unit_off_a_row_with_a_job_is_still_written_whole(tmp_path, monkeypatch):
    s, store = mk(tmp_path, monkeypatch)
    store.add_decision(ticker="P1", action="pass", mode="observe", lane="x", _factors_job=lambda ts: '{"spread": 0.04}')
    assert Store(s.db_path).db.execute("SELECT factors FROM decisions WHERE ticker='P1'").fetchone()["factors"] == '{"spread": 0.04}'
