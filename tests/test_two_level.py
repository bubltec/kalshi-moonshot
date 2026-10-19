import time
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_moonshot import commodities as cm
from kalshi_moonshot import config, lanes, runner
from kalshi_moonshot.estimators import build_estimators
from kalshi_moonshot.jobs import Jobs
from kalshi_moonshot.store import Store
from kalshi_moonshot.worker import Worker
from tests.test_commodities import FakeFeed, feed_series, tail_event


def settings(tmp_path, **over):
    s = config.load()
    base = {
        "data_dir": tmp_path, "mode": "observe", "env": "demo", "skip_categories": frozenset(),
        "commodities_enabled": True, "commodity_min_edge": 0.08, "commodity_min_volume": 0.0,
        "commodity_review_minutes": 2.0, "commodity_scope_minutes": 30.0,
        "commodity_cooldown_minutes": 15.0, "steady_enabled": False, "min_order_usd": 0.0,
    }
    return type(s)(**{**s.__dict__, **base, **over})


class Client:
    """Serves one gold event, by series and by event, and counts the requests."""

    def __init__(self, markets):
        self.markets = markets
        self.calls: list[str] = []

    def event_info(self):
        return {"KXGOLDD-26OCT19": ("Commodities", "KXGOLDD", False)}

    def series_markets(self, series):
        self.calls.append(f"series:{series}")
        return [dict(m) for m in self.markets if m["series_ticker"] == series]

    def event_markets(self, event):
        self.calls.append(f"event:{event}")
        return [dict(m) for m in self.markets if m["event_ticker"] == event]

    def fill_event_info(self, tickers):
        return {}

    def candlesticks(self, *a, **k):
        return []


def live_event(hours=6):
    close = (datetime.now(UTC) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ms = tail_event()
    for m in ms:
        m["close_time"] = close
    return ms


def live_quant():
    series = feed_series(price=4000, per_bar=0.0018)
    shift = time.time() - 60 - series.last_ts
    return cm.CommodityPricer(FakeFeed(cm.Series(
        series.last, series.last_ts + shift, series.closes, [t + shift for t in series.times]
    )))


def test_commodity_is_two_level_and_leaves_the_main_scan(tmp_path):
    s = settings(tmp_path)
    assert [x.name for x in lanes.tiered_lanes(s)] == ["commodity"]
    assert "commodity" not in [x.name for x in lanes.main_lanes(s)]


def test_a_two_level_lane_must_name_what_it_fetches(tmp_path):
    (tmp_path / "strategies.toml").write_text(
        '[lane.x]\nestimator = "llm"\nprice = [0.1, 0.2]\nhours = [1, 2]\nreview_minutes = 1\n'
    )
    with pytest.raises(ValueError, match="review_minutes needs"):
        lanes.lane_specs(settings(tmp_path))


def test_scoping_fetches_series_by_name_and_saves_a_watchlist(tmp_path):
    s = settings(tmp_path)
    store = Store(s.db_path)
    client = Client(live_event())
    n = runner.scope_lane(s, client, store, lanes.spec_for(s, "commodity"))
    assert n == 1
    assert set(client.calls) >= {"series:KXGOLDD", "series:KXWTI"}
    assert not any(c.startswith("event:") for c in client.calls)
    assert "KXGOLDD-26OCT19" in store.watch("commodity")


def test_scoping_keeps_the_old_watchlist_if_every_fetch_fails(tmp_path):
    from kalshi_moonshot.kalshi import KalshiError

    s = settings(tmp_path)
    store = Store(s.db_path)
    store.set_watch("commodity", {"KXGOLDD-26OCT19": {"close": "2099-01-01T00:00:00Z"}})

    class Down(Client):
        def series_markets(self, series):
            raise KalshiError("down")

    with pytest.raises(KalshiError):
        runner.scope_lane(s, Down([]), store, lanes.spec_for(s, "commodity"))
    assert "KXGOLDD-26OCT19" in store.watch("commodity")


def test_reviewing_touches_only_the_watched_events(tmp_path):
    s = settings(tmp_path)
    store = Store(s.db_path)
    client = Client(live_event())
    spec = lanes.spec_for(s, "commodity")
    runner.scope_lane(s, client, store, spec)
    client.calls.clear()
    shots = runner.review_lane(s, client, store, spec, build_estimators(None, None, None, live_quant()))
    assert client.calls == ["event:KXGOLDD-26OCT19"]
    assert len(shots) == 1 and shots[0].lane == "commodity"


def test_cooldown_stops_a_fast_loop_from_relogging_the_same_market(tmp_path):
    s = settings(tmp_path)
    store = Store(s.db_path)
    client = Client(live_event())
    spec = lanes.spec_for(s, "commodity")
    runner.scope_lane(s, client, store, spec)
    est = build_estimators(None, None, None, live_quant())
    first = runner.review_lane(s, client, store, spec, est)
    assert first
    store.add_decision(
        ticker=first[0].ticker, title="t", bid=0.1, ask=0.1, model_p=0.3, edge=0.1,
        action="pass", mode="observe",
    )
    assert runner.review_lane(s, client, store, spec, est) == []


def test_finished_events_leave_the_watchlist(tmp_path):
    s = settings(tmp_path)
    store = Store(s.db_path)
    store.set_watch("commodity", {"OLD": {"close": "2020-01-01T00:00:00Z"}})
    runner.review_lane(s, Client([]), store, lanes.spec_for(s, "commodity"), {})
    assert store.watch("commodity") == {}


def test_higher_priority_jobs_run_first_even_when_queued_later(tmp_path):
    jobs = Jobs(Store(tmp_path / "m.db"))
    jobs.enqueue("evaluate", run_at=time.time() - 100)
    jobs.enqueue("evaluate", {"lane": "fast"}, run_at=time.time() - 1, priority=10)
    assert jobs.claim().payload == {"lane": "fast"}


def test_worker_keeps_a_scope_and_a_review_job_alive_per_tiered_lane(tmp_path):
    s = settings(tmp_path)
    w = Worker(s, Client([]), None, Store(s.db_path), None, quant=live_quant())
    w.start()
    kinds = {k for k, st, _ in w.jobs.counts() if st == "pending"}
    assert {"lane_scope:commodity", "lane_review:commodity", "scan", "review"} <= kinds


def test_scope_job_reschedules_itself_and_pulls_the_review_forward(tmp_path):
    s = settings(tmp_path)
    store = Store(s.db_path)
    w = Worker(s, Client(live_event()), None, store, None, quant=live_quant())
    w.start()
    job = None
    for _ in range(20):
        job = w.jobs.claim()
        assert job is not None
        if job.kind == "lane_scope:commodity":
            break
        w.jobs.complete(job.id)
    assert job.kind == "lane_scope:commodity"
    w.run_job(job)
    rows = store.db.execute(
        "SELECT kind, run_at, priority FROM jobs WHERE status='pending' AND kind LIKE 'lane_%'"
    ).fetchall()
    by = {r["kind"]: r for r in rows}
    assert by["lane_scope:commodity"]["run_at"] > time.time() + 25 * 60
    assert by["lane_review:commodity"]["run_at"] <= time.time() + 1
    assert by["lane_review:commodity"]["priority"] == 10


def test_review_job_queues_prioritized_evaluations_and_reschedules(tmp_path):
    s = settings(tmp_path)
    store = Store(s.db_path)
    w = Worker(s, Client(live_event()), None, store, None, quant=live_quant())
    runner.scope_lane(s, w.client, store, lanes.spec_for(s, "commodity"))
    w.jobs.enqueue("lane_review:commodity", priority=10)
    job = w.jobs.claim()
    w.run_job(job)
    ev = store.db.execute("SELECT priority, expires_ts FROM jobs WHERE kind='evaluate'").fetchall()
    assert len(ev) == 1 and ev[0]["priority"] == 10
    nxt = store.db.execute(
        "SELECT run_at FROM jobs WHERE kind='lane_review:commodity' AND status='pending'"
    ).fetchone()
    assert 100 < nxt["run_at"] - time.time() < 130  # ~2 minutes


def test_disabled_lane_jobs_end_quietly(tmp_path):
    s = settings(tmp_path, commodities_enabled=False)
    w = Worker(s, Client([]), None, Store(s.db_path), None)
    w.jobs.enqueue("lane_review:commodity")
    w.run_job(w.jobs.claim())
    assert not any(k.startswith("lane_review") for k, st, _ in w.jobs.counts() if st == "pending")
