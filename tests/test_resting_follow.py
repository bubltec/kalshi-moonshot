"""A resting buy waits on the websocket and a price change re-evaluates it (CLAUDE.md, 2026-11-19)."""
import threading
import time
from types import SimpleNamespace

import pytest

from kalshi_moonshot import restbuys, runner
from tests.test_buy_retry import RestClient, setup  # noqa: F401  (the fixture)


class Socket(RestClient):
    """A client whose lifecycle stream is up; resting orders get ids r1, r2 ...; the account's fills list holds what `account` says."""

    def __init__(self, asks, account=None):
        super().__init__(fills=[], asks=asks)
        self.cache = SimpleNamespace(fed=lambda: True)
        self.account = account if account is not None else {}
        self.cancels = []
        self.requests = 0

    def buy_resting(self, ticker, side, count, price, seconds, client_order_id=None, post_only=True):
        self.rested.append((count, round(price, 4), client_order_id))
        return {"order_id": f"r{len(self.rested)}"}

    def cancel_order(self, order_id):
        self.cancels.append(order_id)
        return {}

    def portfolio_rows(self, path, key, min_ts=None, pages=1):
        self.requests += 1
        return [{"fill_id": f"{oid}-f", "market_ticker": "KXBTC15M-1", "count_fp": str(n), "yes_price_dollars": "0.3000", "no_price_dollars": "0.7000", "fee_cost": "0",
                 "ts": 1.0, "book_side": "bid", "is_taker": False, "order_id": oid, "action": "buy", "side": "yes"} for oid, n in self.account.items()]


def run_place(s, client, store, shot):
    out = {}
    th = threading.Thread(target=lambda: out.update(got=runner.place(s, client, type(store)(store.path), shot, "why", 1.5, order_key="k")), daemon=True, name="placer")
    th.start()
    return th, out


def wait_for(cond, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    restbuys.reset()
    real = time.sleep

    def sleep(x):
        if threading.current_thread().name == "placer":
            raise AssertionError("a resting buy on the socket sleeps no seconds")
        real(x)

    monkeypatch.setattr(runner.time, "sleep", sleep)
    yield
    restbuys.reset()


def feed(client_ticker="KXBTC15M-1"):
    restbuys.on_event({"kind": "quote", "ticker": "other", "bid": 0.5, "ask": 0.6, "ts": time.time()})  # the socket is feeding


def test_a_streamed_fill_ends_the_wait_at_once_and_the_account_is_read_once(setup):
    s, store, shot, spec = setup
    spec(buy_rest_seconds=60.0, buy_retries=0)
    client = Socket(asks=[0.31], account={"r1": 1.5})
    feed()
    th, out = run_place(s, client, store, shot)
    assert wait_for(lambda: restbuys._by_order.get("r1") is not None)
    t0 = time.time()
    restbuys.on_event({"kind": "fill", "ticker": shot.ticker, "order_id": "r1", "count": 1.5, "action": "buy"})
    th.join(3.0)
    assert not th.is_alive() and time.time() - t0 < 2.0  # not a minute
    assert out["got"].filled == 1.5 and client.requests == 1 and client.cancels == []


def test_a_fall_in_the_ask_moves_the_order_down_when_the_lane_follows(setup):
    s, store, shot, spec = setup
    spec(buy_rest_seconds=60.0, buy_retries=0, buy_rest_follow=True)
    client = Socket(asks=[0.31, 0.27], account={"r2": 1.5})
    feed()
    th, out = run_place(s, client, store, shot)
    assert wait_for(lambda: restbuys._by_order.get("r1") is not None)
    restbuys.on_event({"kind": "quote", "ticker": shot.ticker, "bid": 0.25, "ask": 0.27, "ts": time.time()})
    assert wait_for(lambda: len(client.rested) == 2)
    assert client.cancels == ["r1"]
    assert client.rested[1][1] == 0.26 and client.rested[1][2] == "k-f0"  # one cent under the new ask, a new id from the same key
    restbuys.on_event({"kind": "fill", "ticker": shot.ticker, "order_id": "r2", "count": 1.5, "action": "buy"})
    th.join(3.0)
    assert out["got"].filled == 1.5


def test_a_risen_ask_never_raises_the_price(setup):
    s, store, shot, spec = setup
    spec(buy_rest_seconds=60.0, buy_retries=0, buy_rest_follow=True)
    client = Socket(asks=[0.31, 0.36], account={"r1": 1.5})
    feed()
    th, out = run_place(s, client, store, shot)
    assert wait_for(lambda: restbuys._by_order.get("r1") is not None)
    restbuys.on_event({"kind": "quote", "ticker": shot.ticker, "bid": 0.34, "ask": 0.36, "ts": time.time()})
    time.sleep(0.2)
    assert client.cancels == [] and len(client.rested) == 1
    restbuys.on_event({"kind": "fill", "ticker": shot.ticker, "order_id": "r1", "count": 1.5, "action": "buy"})
    th.join(3.0)
    assert out["got"].filled == 1.5


def test_an_ask_that_falls_under_the_lanes_floor_withdraws_the_order(setup):
    s, store, shot, spec = setup
    spec(buy_rest_seconds=60.0, buy_retries=0, buy_rest_follow=True)
    client = Socket(asks=[0.31, 0.01], account={})
    feed()
    th, out = run_place(s, client, store, shot)
    assert wait_for(lambda: restbuys._by_order.get("r1") is not None)
    restbuys.on_event({"kind": "quote", "ticker": shot.ticker, "bid": 0.0, "ask": 0.01, "ts": time.time()})
    th.join(3.0)
    assert not th.is_alive() and client.cancels == ["r1"] and len(client.rested) == 1 and out["got"].filled == 0


def test_without_the_lane_field_a_quote_wakes_the_wait_and_changes_nothing(setup):
    s, store, shot, spec = setup
    spec(buy_rest_seconds=60.0, buy_retries=0)
    client = Socket(asks=[0.31, 0.27], account={"r1": 1.5})
    feed()
    th, out = run_place(s, client, store, shot)
    assert wait_for(lambda: restbuys._by_order.get("r1") is not None)
    restbuys.on_event({"kind": "quote", "ticker": shot.ticker, "bid": 0.25, "ask": 0.27, "ts": time.time()})
    time.sleep(0.2)
    assert client.cancels == [] and len(client.rested) == 1
    restbuys.on_event({"kind": "fill", "ticker": shot.ticker, "order_id": "r1", "count": 1.5, "action": "buy"})
    th.join(3.0)
    assert out["got"].filled == 1.5


def test_the_stream_filled_figure_stands_when_the_accounts_list_trails_it(setup):
    s, store, shot, spec = setup
    spec(buy_rest_seconds=60.0, buy_retries=0)
    client = Socket(asks=[0.31], account={})  # the list shows nothing yet
    feed()
    th, out = run_place(s, client, store, shot)
    assert wait_for(lambda: restbuys._by_order.get("r1") is not None)
    restbuys.on_event({"kind": "fill", "ticker": shot.ticker, "order_id": "r1", "count": 1.5, "action": "buy"})
    th.join(3.0)
    assert out["got"].filled == 1.5
