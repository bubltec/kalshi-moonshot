import dataclasses
from types import SimpleNamespace

import pytest

from kalshi_moonshot import config, lanes, runner
from kalshi_moonshot.kalshi import KalshiError
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot


class Client:
    """An exchange whose buy answers come from `fills` (a fill count per try); `asks` are the NO-book levels the book shows on each read (the YES ask is 1 minus the best NO bid)."""

    def __init__(self, fills, asks):
        self.fills, self.asks, self.sent, self.reads, self.ids = list(fills), list(asks), [], 0, []

    def orderbook(self, ticker):
        no_bid = 1.0 - self.asks[min(self.reads, len(self.asks) - 1)]
        self.reads += 1
        return {"orderbook_fp": {"no_dollars": [[f"{no_bid:.4f}", "100.00"]]}}

    def buy_yes_ioc(self, ticker, count, price, client_order_id=None):
        self.sent.append((count, round(price, 4)))
        self.ids.append(client_order_id)
        got = self.fills.pop(0) if self.fills else 0.0
        if isinstance(got, Exception):
            raise got
        return {"fill_count": got, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": f"o{len(self.sent)}"}

    def market(self, ticker):
        return {"exchange_index": 0}

    def balance(self):
        return {"balance_breakdown": [{"balance": "50.0000", "exchange_index": 0}]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODE", "trade")
    monkeypatch.setenv("KALSHI_ENV", "demo")
    monkeypatch.setenv("MIN_ORDER_USD", "0.25")
    runner._NO_FRACTION.clear()
    runner._EXCHANGE_OF.clear()
    runner._UNFUNDED.clear()
    s = config.load()
    store = Store(s.db_path)
    monkeypatch.setattr(runner.risk, "check", lambda *a, **k: SimpleNamespace(ok=True, reason=""))
    monkeypatch.setattr(runner, "_top_up", lambda *a, **k: None)
    shot = LongShot("KXBTC15M-1", "t", "r", 0.30, 0.31, 100.0, "2099-01-01T00:00:00Z", 0.2, lane="scalpy", series="KXBTC15M", event_ticker="KXBTC15M-E", side="yes")

    def spec(**over):
        base = dataclasses.replace(lanes.spec_for(s, "moonshot"), buy_rest_seconds=0.0, price=(0.02, 0.39), buy_slip=0.02, buy_retries=1, buy_retry_chase=0.0)
        sp = dataclasses.replace(base, **over)
        monkeypatch.setattr(runner, "lane_specs", lambda settings: {"scalpy": sp})
        return sp

    yield s, store, shot, spec
    runner._NO_FRACTION.clear()


def test_a_buy_that_fills_nothing_is_sent_again_at_the_fresh_ask(setup):
    s, store, shot, spec = setup
    spec()
    client = Client(fills=[0.0, 1.5], asks=[0.30])  # the book still shows 0.30
    got = runner.place(s, client, store, shot, "why", 1.5, order_key="k")
    assert got.filled == 1.5
    assert [c for c, _ in client.sent] == [1.5, 1.5] and client.ids == ["k", "k-r1"]  # the retry has its own id: Kalshi refuses one it has seen


def test_a_retry_is_not_sent_when_the_ask_ran_past_the_first_limit(setup):
    s, store, shot, spec = setup
    spec()
    client = Client(fills=[0.0, 1.5], asks=[0.30, 0.36])  # place() reads 0.30; by the retry the ask is 0.36, a limit of 0.38 over the first 0.32
    got = runner.place(s, client, store, shot, "why", 1.5)
    assert got.filled == 0 and len(client.sent) == 1


def test_a_retry_may_chase_by_the_lanes_allowance(setup):
    s, store, shot, spec = setup
    spec(buy_retry_chase=0.06)
    client = Client(fills=[0.0, 1.5], asks=[0.30, 0.36])
    got = runner.place(s, client, store, shot, "why", 1.5)
    assert got.filled == 1.5 and client.sent[1][1] == pytest.approx(0.38)


def test_a_retry_stays_inside_the_lanes_price_band(setup):
    s, store, shot, spec = setup
    spec(price=(0.02, 0.31), buy_retry_chase=0.5)
    client = Client(fills=[0.0, 1.5], asks=[0.30, 0.33])  # over the lane's 0.31 ceiling by the retry
    got = runner.place(s, client, store, shot, "why", 1.5)
    assert got.filled == 0 and len(client.sent) == 1


def test_no_retry_without_the_setting(setup):
    s, store, shot, spec = setup
    spec(buy_retries=0)
    client = Client(fills=[0.0, 1.5], asks=[0.30])
    got = runner.place(s, client, store, shot, "why", 1.5)
    assert got.filled == 0 and len(client.sent) == 1


def test_it_tries_at_most_the_set_number_of_times(setup):
    s, store, shot, spec = setup
    spec(buy_retries=2)
    client = Client(fills=[0.0, 0.0, 0.0, 1.5], asks=[0.30])
    got = runner.place(s, client, store, shot, "why", 1.5)
    assert got.filled == 0 and len(client.sent) == 3  # the first try and two retries


def test_a_retry_that_the_risk_gate_refuses_is_not_sent(setup, monkeypatch):
    s, store, shot, spec = setup
    spec()
    verdicts = iter([True, False])
    monkeypatch.setattr(runner.risk, "check", lambda *a, **k: SimpleNamespace(ok=next(verdicts), reason="cap"))
    client = Client(fills=[0.0, 1.5], asks=[0.30])
    got = runner.place(s, client, store, shot, "why", 1.5)
    assert got.filled == 0 and len(client.sent) == 1


def test_a_failed_retry_order_is_reported_and_does_not_raise(setup):
    s, store, shot, spec = setup
    spec()
    client = Client(fills=[0.0, KalshiError("POST -> 400: invalid_price")], asks=[0.30])
    got = runner.place(s, client, store, shot, "why", 2.0)
    assert got.filled == 0 and "order failed" in got.note and len(client.sent) == 2


def test_a_buy_is_skipped_when_the_ask_fell_far_under_the_decision(setup):
    s, store, shot, spec = setup
    spec(buy_max_move=0.05, buy_retries=0)
    client = Client(fills=[1.5], asks=[0.20])  # decided at 0.31; the book now offers 0.20
    got = runner.place(s, client, store, shot, "why", 1.5)
    assert got.filled == 0 and "fell from 0.31 to 0.20" in got.note and client.sent == []  # nothing was ordered
    assert not got.note.startswith(runner_sent_nothing())  # not a miss the hunt would retry


def test_a_small_fall_still_buys(setup):
    s, store, shot, spec = setup
    spec(buy_max_move=0.05, buy_retries=0)
    client = Client(fills=[1.5], asks=[0.27])  # 4c under the decision: inside the 5c the lane allows
    got = runner.place(s, client, store, shot, "why", 1.5)
    assert got.filled == 1.5 and len(client.sent) == 1


def test_a_rise_is_left_to_the_limit_not_the_guard(setup):
    s, store, shot, spec = setup
    spec(buy_max_move=0.05, buy_retries=0)
    client = Client(fills=[1.5], asks=[0.31])  # at the decision price: nothing for the guard to do
    got = runner.place(s, client, store, shot, "why", 1.5)
    assert got.filled == 1.5


def test_the_guard_is_off_unless_the_lane_sets_it(setup):
    s, store, shot, spec = setup
    spec(buy_retries=0)
    client = Client(fills=[1.5], asks=[0.20])
    got = runner.place(s, client, store, shot, "why", 1.5)
    assert got.filled == 1.5


def runner_sent_nothing():
    from kalshi_moonshot import buyer

    return buyer.SENT_NOTHING
