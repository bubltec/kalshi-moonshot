from datetime import UTC, datetime, timedelta

import pytest

from kalshi_moonshot import config, runner
from kalshi_moonshot.kalshi import KalshiError, KalshiRateLimited
from kalshi_moonshot.store import Store
from kalshi_moonshot.worker import Worker


def settings(tmp_path, **over):
    s = config.load()
    base = {
        "data_dir": tmp_path, "mode": "observe", "env": "demo", "min_price": 0.02, "max_price": 0.10,
        "min_volume": 100.0, "max_spread": 0.03, "min_hours_to_close": 2.0, "max_hours_to_close": 24.0,
        "min_edge": 0.05, "shortlist": 5, "steady_enabled": False, "skip_categories": frozenset(),
        "skip_numeric": True, "scan_interval_minutes": 30, "job_max_attempts": 3,
        "max_usd_per_trade": 1.0, "max_loss_per_day": 10.0, "max_open_positions": 5,
    }
    return type(s)(**{**s.__dict__, **base, **over})


def mk(ticker, ask="0.05", hours=10):
    close = (datetime.now(UTC) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "ticker": ticker, "title": f"Will {ticker} happen?", "market_type": "binary",
        "status": "active", "yes_bid_dollars": "0.04", "yes_ask_dollars": ask,
        "volume_fp": "900", "close_time": close, "rules_primary": "rules",
        "event_ticker": "EV",
    }


class FakeClient:
    def __init__(self, tickers=("AAA", "BBB")):
        self.tickers = tickers
        self.fresh_ask = "0.05"
        self.market_error: Exception | None = None
        self.buy_error: Exception | None = None
        self.bought: list[tuple] = []
        self.sold: list[tuple] = []
        self.bid = "0.04"

    def event_info(self):
        return {"EV": ("Politics", "KXP")}

    def markets_closing(self, lo, hi):
        for t in self.tickers:
            yield mk(t)

    def market(self, ticker):
        if self.market_error:
            raise self.market_error
        m = mk(ticker, ask=self.fresh_ask)
        m["yes_bid_dollars"] = self.bid
        return m

    def candlesticks(self, series, ticker, hours=6):
        return []

    def orderbook(self, ticker):
        return {"orderbook_fp": {"no_dollars": [["0.9500", "100.00"]]}}

    def buy_yes_ioc(self, ticker, count, price, client_order_id=None):
        if self.buy_error:
            raise self.buy_error
        self.bought.append((ticker, count, client_order_id))
        return {"fill_count": count, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "o1"}

    def sell_yes_ioc(self, ticker, count, price, client_order_id=None):
        self.sold.append((ticker, count, price))
        return {"fill_count": count, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "s1"}


class FakeEst:
    def __init__(self, p=0.3):
        self.p = p
        self.fail = False
        self.calls = 0

    def estimate(self, title, rules, close_time, similar="", context="", subtitle=""):
        self.calls += 1
        if self.fail:
            raise RuntimeError("ollama down")
        return self.p, "because"


def scan_once(w):
    """What the all-markets scan used to do (removed 2026-11-23): shortlist the fake client's open markets for the moonshot spec and queue one evaluate job per shot. The evaluation path is what these tests exercise; the scan that fed it is gone, so the test queues the jobs itself."""
    from dataclasses import asdict

    from kalshi_moonshot.lanes import job_priority, spec_for

    info = w.client.event_info()
    markets = []
    for m in w.client.markets_closing(0, 1):
        category, series, mutex = (*info.get(m.get("event_ticker", ""), ("", "", False)), "", "", False)[:3]
        m["category"], m["mutually_exclusive"] = category, bool(mutex)
        if series:
            m.setdefault("series_ticker", series)
        markets.append(m)
    spec = spec_for(w.s, "moonshot")
    for shot in runner.lane_shots(w.s, w.client, markets, spec, w.estimators()):
        w.jobs.enqueue(
            "evaluate", {"shot": asdict(shot)}, dedupe_key=f"evaluate:{shot.lane}:{shot.ticker}",
            expires_in=w.s.scan_interval_minutes * 60, max_attempts=w.s.job_max_attempts, priority=job_priority(w.s, spec),
        )


def worker(tmp_path, client=None, est=None, **over):
    s = settings(tmp_path, **over)
    w = Worker(s, client or FakeClient(), est or FakeEst(), Store(s.db_path), None)
    start = w.start
    w.start = lambda: (start(), scan_once(w))[0]  # starting the bot used to queue the first scan; the test queues its evaluations the same way
    return w


def drain(w, limit=50):
    from kalshi_moonshot import store as store_mod

    for _ in range(limit):
        if not w.step():
            store_mod.flush_decisions(5.0)  # the running worker writes its pass rows from a background thread (store.queue_decisions): idle means those are written too
            return
    raise AssertionError("worker never went idle")


def status(w, kind):
    return {st: n for k, st, n in w.jobs.counts() if k == kind}


def test_a_scan_queues_evaluations_and_the_next_scan(tmp_path):
    w = worker(tmp_path)
    w.start()
    drain(w)
    assert status(w, "evaluate") == {"done": 2}
    assert status(w, "resolve") == {"done": 1, "pending": 1}  # the next one is waiting (resolve keeps its own schedule since the scan went)
    assert len(w.store.recent_decisions()) == 2


def test_a_model_failure_is_retried_not_logged_as_a_decision(tmp_path):
    est = FakeEst()
    est.fail = True
    w = worker(tmp_path, est=est)
    w.start()
    drain(w)
    assert status(w, "evaluate") == {"pending": 2}  # backing off
    assert w.store.recent_decisions() == []
    est.fail = False
    w.jobs.db.execute("UPDATE jobs SET run_at=0 WHERE status='pending' AND kind='evaluate'")
    drain(w)
    assert status(w, "evaluate") == {"done": 2}
    assert len(w.store.recent_decisions()) == 2


def test_the_last_attempt_logs_a_model_error_pass(tmp_path):
    est = FakeEst()
    est.fail = True
    w = worker(tmp_path, est=est, job_max_attempts=2)
    w.start()
    drain(w)
    w.jobs.db.execute("UPDATE jobs SET run_at=0 WHERE status='pending' AND kind='evaluate'")
    drain(w)
    rows = w.store.recent_decisions()
    assert len(rows) == 2 and all("model error" in r["reason"] for r in rows)


def test_a_price_that_moved_out_of_range_skips_the_model(tmp_path):
    client, est = FakeClient(), FakeEst()
    w = worker(tmp_path, client, est)
    w.start()
    client.fresh_ask = "0.50"
    drain(w)
    assert est.calls == 0 and w.store.recent_decisions() == []
    assert status(w, "evaluate") == {"done": 2}


def test_rate_limit_defers_without_using_an_attempt(tmp_path):
    client = FakeClient()
    w = worker(tmp_path, client)
    w.start()
    w.step()
    client.market_error = KalshiRateLimited("slow down", retry_after=60)
    w.step()
    row = w.jobs.db.execute("SELECT status, attempts FROM jobs WHERE kind='evaluate' AND attempts=0").fetchone()
    assert row is not None and row["status"] == "pending"


def test_restart_resumes_jobs_that_were_running(tmp_path):
    w = worker(tmp_path)
    w.start()
    interrupted = w.jobs.claim(kinds={"evaluate"})  # one is picked up... and the bot dies
    w2 = worker(tmp_path)  # new process, same database
    w2.start()
    drain(w2)
    row = w2.jobs.db.execute("SELECT status FROM jobs WHERE id=?", (interrupted.id,)).fetchone()
    assert row["status"] == "done"
    assert "running" not in status(w2, "evaluate")
    assert {r["ticker"] for r in w2.store.recent_decisions()} == {"AAA", "BBB"}


def test_an_error_mid_order_parks_the_job_and_never_double_buys(tmp_path):
    client = FakeClient(("AAA",))
    client.buy_error = RuntimeError("connection reset after send")
    w = worker(tmp_path, client, mode="trade")
    w.start()
    drain(w)
    dead = w.jobs.dead()
    assert len(dead) == 1 and "interrupted while placing" in dead[0]["last_error"]
    assert client.bought == []
    assert w.jobs.retry_dead()[0] == 0


def test_orders_use_a_stable_id_per_job(tmp_path):
    client = FakeClient(("AAA",))
    w = worker(tmp_path, client, mode="trade")
    w.start()
    drain(w)
    assert len(client.bought) == 1
    ticker, _, order_id = client.bought[0]
    assert ticker == "AAA" and len(order_id) == 36  # a UUID derived from the job, not random


def test_the_main_loop_leaves_evaluate_jobs_to_the_buy_workers(tmp_path):
    w = worker(tmp_path)
    w.jobs.enqueue("evaluate", {"shot": {}}, run_at=1.0)
    w._split = True
    w._buy_threads = 2
    claimed = w.jobs.claim(exclude=(__import__("kalshi_moonshot.worker", fromlist=["x"]).MAINTENANCE | frozenset({"evaluate"})))
    assert claimed is None  # the main loop skips it
    assert w.jobs.claim(kinds={"evaluate"}).kind == "evaluate"  # a buy worker takes it


def test_without_buy_workers_the_main_loop_still_runs_evaluate_jobs(tmp_path):
    w = worker(tmp_path, buy_workers=0)
    w.start()
    w._split = True
    drain(w)
    assert status(w, "evaluate") == {"done": 2}


def test_a_buy_worker_runs_only_evaluate_jobs(tmp_path):
    w = worker(tmp_path)
    w.start()
    w.jobs.enqueue("lane_scope:x", {}, run_at=1.0)
    ran: list[str] = []

    def record(job):
        ran.append(job.kind)
        w.jobs.complete(job.id, "")
        w.stopping = len(ran) >= 2  # stop once both evaluations have gone through

    w.run_job = record
    w._buy_loop()
    assert ran == ["evaluate", "evaluate"]
    assert status(w, "lane_scope:x") == {"pending": 1}  # a buy worker leaves every other kind alone


def test_one_lane_gets_one_lock_and_lanes_do_not_share(tmp_path):
    assert runner.lane_lock("a") is runner.lane_lock("a")
    assert runner.lane_lock("a") is not runner.lane_lock("b")


def test_changing_the_request_rate_keeps_the_same_client_open(tmp_path):
    from kalshi_moonshot.kalshi import KalshiClient

    client = KalshiClient("https://demo.test", max_rps=3.0)
    s = settings(tmp_path, max_rps=3.0)
    faster = settings(tmp_path, max_rps=8.0)
    w = Worker(s, client, FakeEst(), Store(s.db_path), None, client_factory=lambda fresh: KalshiClient("https://other.test"), reload=lambda: faster)
    w.refresh()
    assert w.client is client and not client._http.is_closed  # nothing was swapped or closed under the other threads
    assert abs(client._gap - 1 / 8.0) < 1e-9


def test_a_reload_reaches_the_tape_recorder(tmp_path):
    from types import SimpleNamespace

    s = settings(tmp_path)
    fresh = settings(tmp_path, max_rps=8.0)
    w = Worker(s, FakeClient(), FakeEst(), Store(s.db_path), None, reload=lambda: fresh)
    w.recorder = SimpleNamespace(s=s)
    w.refresh()
    assert w.recorder.s is fresh  # WATCH_SERIES edits reach the tape without a restart


def test_a_new_key_swaps_the_client_without_closing_the_old_one(tmp_path):
    from kalshi_moonshot.kalshi import KalshiClient

    client = KalshiClient("https://demo.test", max_rps=3.0)
    s = settings(tmp_path)
    rekeyed = settings(tmp_path, key_id="another")
    fresh_client = KalshiClient("https://demo.test", max_rps=3.0)
    w = Worker(s, client, FakeEst(), Store(s.db_path), None, client_factory=lambda fresh: fresh_client, reload=lambda: rekeyed)
    w.refresh()
    assert w.client is fresh_client and not client._http.is_closed  # threads still holding the old one keep working


def test_a_live_lane_is_queued_ahead_of_a_paper_lane_whatever_the_priority_numbers_say():
    from types import SimpleNamespace

    from kalshi_moonshot.lanes import LIVE_BOOST, job_priority

    s = SimpleNamespace(live_lanes=frozenset({"live_a"}), is_live=lambda name, series="": name == "live_a")
    live = SimpleNamespace(name="live_a", priority=46)
    paper = SimpleNamespace(name="paper_b", priority=48)  # the paper lane's own number is the higher one
    assert job_priority(s, live) == 46 + LIVE_BOOST > job_priority(s, paper) == 48
    s_all = SimpleNamespace(live_lanes=None, is_live=lambda name, series="": True)  # an empty LIVE_LANES means every lane: nothing to rank
    assert job_priority(s_all, live) == 46


def test_a_live_lane_thread_runs_only_its_own_lanes_jobs(tmp_path):
    w = worker(tmp_path)
    w.jobs.enqueue("lane_scope:paper_b", {}, run_at=1.0, priority=900)
    w.jobs.enqueue("lane_scope:live_a", {}, run_at=1.0)
    w.jobs.enqueue("evaluate", {"shot": {}}, run_at=1.0)
    ran: list[str] = []

    def record(job):
        ran.append(job.kind)
        w.jobs.complete(job.id, "")
        w.stopping = True

    w.run_job = record
    w._lane_loop("live_a")
    assert ran == ["lane_scope:live_a"]
    assert status(w, "lane_scope:paper_b") == {"pending": 1} and status(w, "evaluate") == {"pending": 1}
