"""A lane with entry_socket is scoped and reviewed by the stream's events, not by timers (2026-11-14, user: "we need to get rid of these timers we added all over the place. its crazy").
Unit: the worker's rules for when a timer is kept. Integration: lifecycle JSON through the stream and the tape recorder into the scope jobs; an index tick through the entry watcher into a review."""

import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from kalshi_moonshot import entrywatch, kalshiws, lanes, runner, tape
from kalshi_moonshot.jobs import Jobs
from kalshi_moonshot.marketdata import MarketCache
from kalshi_moonshot.store import Store
from kalshi_moonshot.worker import Worker
from tests.test_entrywatch import socket_settings, watcher
from tests.test_two_level import Client, live_event, live_quant


class CachedClient(Client):
    def __init__(self, markets):
        super().__init__(markets)
        self.cache = MarketCache()


def make(tmp_path):
    s = socket_settings(tmp_path)
    client = CachedClient(live_event())
    w = Worker(s, client, None, Store(s.db_path), None, quant=live_quant())
    spec = lanes.lane_specs(s)["commodity"]
    assert lanes.tiered(spec)
    return s, w, client, spec


def pending(w, kind):
    rows = w.jobs.db.execute("SELECT run_at FROM jobs WHERE kind=? AND status='pending'", (kind,)).fetchall()
    return [r["run_at"] for r in rows]


def test_a_lane_is_stream_driven_only_while_the_stream_is_confirmed(tmp_path):
    _, w, client, spec = make(tmp_path)
    assert not w._driven(spec)
    client.cache.feed(True)
    assert w._driven(spec)
    assert not w._driven(replace(spec, review_minutes=None))  # a lane that is not two-level keeps its timers
    client.cache.feed(False)
    assert not w._driven(spec)


def test_a_driven_lane_gets_no_timer_job_and_an_undriven_one_does(tmp_path):
    _, w, client, _ = make(tmp_path)
    w._ensure_tiered()
    assert pending(w, "lane_scope:commodity") and pending(w, "lane_review:commodity")
    w.jobs.db.execute("DELETE FROM jobs WHERE kind LIKE 'lane_%'")
    w.jobs.db.commit()
    client.cache.feed(True)
    w._ensure_tiered()
    assert not pending(w, "lane_scope:commodity") and not pending(w, "lane_review:commodity")


def test_a_scope_run_reschedules_itself_only_when_the_stream_is_down(tmp_path):
    s, w, client, _ = make(tmp_path)
    for up in (False, True):
        client.cache.feed(up)
        w.jobs.db.execute("DELETE FROM jobs WHERE kind LIKE 'lane_%'")
        w.jobs.db.commit()
        w.jobs.enqueue("lane_scope:commodity", run_at=time.time())
        job = w.jobs.claim(kinds={"lane_scope:commodity"})
        w.run_job(job)
        assert bool(pending(w, "lane_scope:commodity")) is (not up)  # the timer is only a fallback for a down stream
        assert bool(pending(w, "lane_review:commodity"))  # the scope pulls its review forward either way


def test_a_review_run_reschedules_itself_only_when_the_stream_is_down(tmp_path):
    s, w, client, _ = make(tmp_path)
    runner.scope_lane(s, client, w.store if hasattr(w, "store") else Store(s.db_path), lanes.lane_specs(s)["commodity"])
    for up in (False, True):
        client.cache.feed(up)
        w.jobs.db.execute("DELETE FROM jobs WHERE kind LIKE 'lane_%'")
        w.jobs.db.commit()
        w.jobs.enqueue("lane_review:commodity", run_at=time.time())
        w.run_job(w.jobs.claim(kinds={"lane_review:commodity"}))
        assert bool(pending(w, "lane_review:commodity")) is (not up)


def test_a_new_market_scopes_the_lanes_that_trade_its_series_and_a_pause_does_not(tmp_path):
    s, w, client, spec = make(tmp_path)
    client.cache.feed(True)
    series = sorted(runner.lane_series(spec, s))[0]
    jobs = Jobs(Store(s.db_path))
    w._on_market_event(jobs, "market", {"event": "deactivated", "ticker": f"{series}-26NOV141200-00"})
    assert not pending(w, "lane_scope:commodity")  # a pause changes no watchlist
    w._on_market_event(jobs, "market", {"event": "created", "ticker": "KXOTHER-26NOV141200-00"})
    assert not pending(w, "lane_scope:commodity")  # not this lane's series
    w._on_market_event(jobs, "market", {"event": "created", "ticker": f"{series}-26NOV141200-00"})
    assert pending(w, "lane_scope:commodity")


def test_the_stream_coming_up_scopes_every_driven_lane_and_going_down_puts_the_timers_back(tmp_path):
    s, w, client, _ = make(tmp_path)
    jobs = Jobs(Store(s.db_path))
    client.cache.feed(True)
    w._on_market_event(jobs, "feed", True)
    assert pending(w, "lane_scope:commodity")
    w.jobs.db.execute("DELETE FROM jobs WHERE kind LIKE 'lane_%'")
    w.jobs.db.commit()
    client.cache.feed(False)
    w._on_market_event(jobs, "feed", False)
    assert pending(w, "lane_scope:commodity") and pending(w, "lane_review:commodity")


def test_raw_lifecycle_json_through_the_recorder_reaches_the_scope_jobs(tmp_path):
    s, w, client, spec = make(tmp_path)
    series = sorted(runner.lane_series(spec, s))[0]
    got = []
    rec = SimpleNamespace(tape=SimpleNamespace(add=lambda e: None), client=client, listeners=[], market_listeners=[got.append], _wanted=set(), _refresh_wake=__import__("threading").Event(),
                          stream=SimpleNamespace(_tickers=set(), set_tickers=lambda t: None))
    rec._feed_state = lambda up: tape.TapeRecorder._feed_state(rec, up)
    rec._on_lifecycle = lambda e: tape.TapeRecorder._on_lifecycle(rec, e)
    rec._follow_new = lambda t: tape.TapeRecorder._follow_new(rec, t)
    stream = kalshiws.KalshiStream(client, lambda e: tape.TapeRecorder._on_event(rec, e), on_feed=rec._feed_state)
    stream.handle(json.dumps({"type": "subscribed", "id": 6, "msg": {"channel": "market_lifecycle_v2", "sid": 6}}))
    stream.handle(json.dumps({"type": "market_lifecycle_v2", "sid": 6, "seq": 1, "msg": {"event_type": "created", "market_ticker": f"{series}-26NOV141200-00", "close_ts": 5, "open_ts": 1, "additional_metadata": {"event_ticker": f"{series}-26NOV141200"}}}))
    assert [e["event"] for e in got] == ["created"]
    w._on_market_event(Jobs(Store(s.db_path)), "market", got[0])
    assert pending(w, "lane_scope:commodity")


def test_a_created_market_of_a_recorded_series_is_streamed_at_once(tmp_path):
    woke = []
    streamed = []
    rec = SimpleNamespace(_wanted={"KXBTC15M"}, stream=SimpleNamespace(_tickers={"A"}, set_tickers=streamed.append), _refresh_wake=SimpleNamespace(set=lambda: woke.append(1)))
    tape.TapeRecorder._follow_new(rec, "KXBTC15M-26NOV141200-00")
    tape.TapeRecorder._follow_new(rec, "KXFOO-26NOV141200-00")  # a series we do not record
    assert streamed == [{"A", "KXBTC15M-26NOV141200-00"}] and woke == [1]


# ---- the index tick trigger ----
def index_event(idx="BRTI"):
    return {"kind": "index", "index_id": idx, "value": 100.0, "ts": time.time()}


def test_an_index_tick_reviews_the_events_that_index_settles_when_their_quote_is_in_band(tmp_path, monkeypatch):
    s = socket_settings(tmp_path)
    w = watcher(s, CachedClient(live_event()))
    spec = lanes.lane_specs(s)["commodity"]
    ticker = "KXBTC15M-26NOV141200-00"
    w.watch = {("commodity", "KXBTC15M-26NOV141200"): {"series": "KXBTC15M", "ticker": ticker, "close": (datetime.now(UTC) + timedelta(minutes=8)).isoformat()}}
    w._hist[ticker] = __import__("collections").deque([(time.time(), 0.85, 0.87)])
    w.on_event(index_event("ETHUSD_RTI"))  # another coin's index
    assert w._jobs.qsize() == 0
    monkeypatch.setattr(entrywatch, "in_window", lambda *a, **k: True)  # the entry window is covered by its own tests; this one is about the trigger
    w.on_event(index_event("BRTI"))
    w.on_event(index_event("BRTI"))  # inside the gap: no second review
    assert w._jobs.qsize() == 1
    w2 = watcher(s, CachedClient(live_event()))
    w2.watch = dict(w.watch)
    w2._hist[ticker] = __import__("collections").deque([(time.time(), 0.40, 0.45)])  # not in the band
    w2.on_event(index_event("BRTI"))
    assert w2._jobs.qsize() == 0
    assert lanes.tiered(spec)


def test_a_window_opening_or_closing_scopes_the_lane_even_with_no_created_event(tmp_path):
    """The live miss: at 08:00 a window opened with a strike event and the old one closed with a result, and no created or activated event came, so the lane kept a watchlist of closed events."""
    s, w, client, spec = make(tmp_path)
    client.cache.feed(True)
    series = sorted(runner.lane_series(spec, s))[0]
    got = []
    rec = SimpleNamespace(tape=SimpleNamespace(add=lambda e: None), client=client, listeners=[], market_listeners=[got.append], _wanted=set(), _refresh_wake=__import__("threading").Event(), stream=SimpleNamespace(_tickers=set(), set_tickers=lambda t: None))
    rec._follow_new = lambda t: tape.TapeRecorder._follow_new(rec, t)
    rec._on_lifecycle = lambda e: tape.TapeRecorder._on_lifecycle(rec, e)
    jobs = Jobs(Store(s.db_path))
    for ev in ({"kind": "strike", "ts": 1.0, "ticker": f"{series}-26NOV140815-15", "strike": 5.0}, {"kind": "result", "ts": 1.0, "ticker": f"{series}-26NOV140800-00", "result": "yes"}):
        w.jobs.db.execute("DELETE FROM jobs WHERE kind LIKE 'lane_%'")
        w.jobs.db.commit()
        tape.TapeRecorder._on_event(rec, ev)
        assert len(got) >= 1
        w._on_market_event(jobs, "market", got[-1])
        assert pending(w, "lane_scope:commodity"), ev["kind"]
