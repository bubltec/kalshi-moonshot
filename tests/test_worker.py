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


def worker(tmp_path, client=None, est=None, **over):
    s = settings(tmp_path, **over)
    return Worker(s, client or FakeClient(), est or FakeEst(), Store(s.db_path), None)


def drain(w, limit=50):
    for _ in range(limit):
        if not w.step():
            return
    raise AssertionError("worker never went idle")


def status(w, kind):
    return {st: n for k, st, n in w.jobs.counts() if k == kind}


def test_a_scan_queues_evaluations_and_the_next_scan(tmp_path):
    w = worker(tmp_path)
    w.start()
    drain(w)
    assert status(w, "evaluate") == {"done": 2}
    assert status(w, "resolve") == {"done": 1}
    assert status(w, "scan") == {"done": 1, "pending": 1}  # the next one is waiting
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
    w.step()  # the scan
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
    w.step()  # scan queues two evaluations
    interrupted = w.jobs.claim()  # one is picked up... and the bot dies
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


def test_partial_fetch_is_kept_only_when_allowed():
    class Flaky(FakeClient):
        def markets_closing(self, lo, hi):
            yield mk("AAA")
            raise KalshiError("page 2 failed")

    s = config.load()
    assert [m["ticker"] for m in runner.candidate_markets(s, Flaky(), partial_ok=True)] == ["AAA"]
    with pytest.raises(KalshiError):
        runner.candidate_markets(s, Flaky(), partial_ok=False)


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
    w.run_job(w.jobs.claim(kinds={"scan"}))  # the scan queues the two evaluations
    w._split = True
    drain(w)
    assert status(w, "evaluate") == {"done": 2}


def test_a_buy_worker_runs_only_evaluate_jobs(tmp_path):
    w = worker(tmp_path)
    w.start()
    w.run_job(w.jobs.claim(kinds={"scan"}))  # the scan queues the two evaluations
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


def test_the_main_loop_leaves_the_stop_sweep_to_the_sell_worker(tmp_path):
    from kalshi_moonshot.worker import BUY_KINDS, MAINTENANCE, SELL_KINDS

    w = worker(tmp_path)
    w.jobs.enqueue("stop_loss", {}, run_at=1.0)
    w._split = True
    w._buy_threads = 3
    w._sell_threads = 1
    assert w.jobs.claim(exclude=MAINTENANCE | BUY_KINDS | SELL_KINDS) is None
    assert w.jobs.claim(kinds=SELL_KINDS).kind == "stop_loss"


def test_a_sell_worker_runs_only_the_stop_sweep(tmp_path):
    w = worker(tmp_path)
    w.jobs.enqueue("evaluate", {"shot": {}}, run_at=1.0)
    w.jobs.enqueue("stop_loss", {}, run_at=1.0)
    ran: list[str] = []

    def record(job):
        ran.append(job.kind)
        w.jobs.complete(job.id, "")
        w.stopping = True

    w.run_job = record
    w._sell_loop()
    assert ran == ["stop_loss"]
    assert status(w, "evaluate") == {"pending": 1}
