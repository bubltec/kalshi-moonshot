from contextlib import contextmanager

import httpx

from kalshi_moonshot import config
from kalshi_moonshot.kalshi import KalshiClient
from kalshi_moonshot.store import Store
from kalshi_moonshot.worker import MAINTENANCE, Worker


class Client:
    def __init__(self):
        self.in_background = False

    @contextmanager
    def background(self):
        self.in_background = True
        try:
            yield
        finally:
            self.in_background = False


def worker(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    w = Worker(config.load(), Client(), None, Store(tmp_path / "m.db", frozenset()), None)
    w.ran = []
    w.run_job = lambda job: w.ran.append((job.kind, w.client.in_background))
    w.refresh = lambda: None
    return w


def test_the_long_background_jobs_are_the_ones_named_as_maintenance():
    assert {"history", "resolve", "account_sync", "patterns", "review", "scan"} == MAINTENANCE
    assert not MAINTENANCE & {"evaluate", "stop_loss", "lane_scope", "lane_review"}


def test_once_the_maintenance_thread_runs_the_main_loop_leaves_those_jobs_alone(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    w.jobs.enqueue("history", {}, run_at=1.0)
    w.jobs.enqueue("evaluate", {}, run_at=2.0)
    w._split = True
    assert w.step() is True and w.ran == [("evaluate", False)]  # the main loop took the trading job
    assert w.step() is False  # and found nothing else it may take: history is not its job now
    assert w._maintenance_once() is True and w.ran[-1] == ("history", True)  # the maintenance thread took it, throttled
    assert w._maintenance_once() is False


def test_without_the_split_a_single_loop_still_runs_everything(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    w.jobs.enqueue("history", {}, run_at=1.0)
    assert w._split is False and w.step() is True and w.ran == [("history", False)]


def test_maintenance_requests_leave_two_thirds_of_the_request_budget_to_everyone_else(monkeypatch):
    c = KalshiClient("https://example.test", max_rps=100)
    c._http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"markets": [], "cursor": ""})))
    slept = []
    monkeypatch.setattr("kalshi_moonshot.kalshi.time.sleep", lambda t: slept.append(round(t, 4)))
    c.series_markets("S")
    assert 0.02 not in slept  # a normal call is not delayed
    with c.background():
        c.series_markets("S")
    assert 0.02 in slept  # two request gaps of throttling on a background call
    slept.clear()
    c.series_markets("S")
    assert 0.02 not in slept  # and the block ends cleanly
