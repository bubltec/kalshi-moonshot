import time

import pytest

from kalshi_moonshot import config, runner
from kalshi_moonshot.kalshi import KalshiError
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot


class Client:
    def __init__(self):
        self.orders = 0

    def orderbook(self, ticker):
        return {"orderbook_fp": {"no_dollars": [["0.7000", "100.00"]]}}

    def buy_yes_ioc(self, ticker, count, price, client_order_id=None):
        self.orders += 1
        raise KalshiError('POST /portfolio/events/orders -> 400: {"error":{"code":"insufficient_balance"}}')

    def market(self, ticker):
        return {"exchange_index": 0}

    def balance(self):
        return {"balance_breakdown": [{"balance": "0.0000", "exchange_index": 0}, {"balance": "78.5721", "exchange_index": 2}]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODE", "trade")
    monkeypatch.setenv("KALSHI_ENV", "demo")
    monkeypatch.setenv("MIN_ORDER_USD", "0.25")
    runner._EXCHANGE_OF.clear()
    runner._UNFUNDED.clear()
    s = config.load()
    shot = LongShot("KXEURUSD15M-1", "t", "r", 0.29, 0.30, 100.0, "2099-01-01T00:00:00Z", 0.2,
                    lane="fx_ticket", series="KXEURUSD15M", event_ticker="KXEURUSD15M-E", side="yes")
    return s, Store(s.db_path), shot


def test_a_refusal_for_an_unfunded_exchange_is_explained_and_paused(setup):
    s, store, shot = setup
    client = Client()
    first = runner.place(s, client, store, shot, "why", 3)
    assert "exchange 0" in first.note and "$0.00" in first.note and "exchange 2: $78.57" in first.note
    assert client.orders == 1
    second = runner.place(s, client, store, shot, "why", 3)  # no new order is sent during the pause
    assert "no cash" in second.note and client.orders == 1


def test_the_pause_ends(setup):
    s, store, shot = setup
    client = Client()
    runner.place(s, client, store, shot, "why", 3)
    runner._UNFUNDED[0] = time.time() - 1
    runner.place(s, client, store, shot, "why", 3)
    assert client.orders == 2
