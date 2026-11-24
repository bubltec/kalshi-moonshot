"""A resting order is updated in place (CLAUDE.md architecture rule, 2026-11-22; user: "if it finds a different entry point after we placed an order we should just update the order and not cancel"). The client's amend call, the live resting buy's follow rule and the hand-opened position's resting sell all move the order with Kalshi's V2 amend, and fall back to cancel and place when the exchange refuses it."""

import time

import pytest

from kalshi_moonshot import manualrest, restbuys, runner
from kalshi_moonshot.kalshi import KalshiClient, KalshiError
from tests.test_buy_retry import setup  # noqa: F401  (the fixture)
from tests.test_manual_rest import TK, Book
from tests.test_resting_follow import Socket, clean, feed, run_place, wait_for  # noqa: F401  (clean: the autouse fixture)


def client_with(responder):
    c = KalshiClient.__new__(KalshiClient)  # no keys: only the request is under test
    calls = []

    def request(method, path, *, params=None, json=None, auth=False):
        calls.append((method, path, json, auth))
        return responder(path, json)

    c._request = request
    return c, calls


def test_the_amend_names_the_yes_leg_the_way_the_resting_orders_do():
    c, calls = client_with(lambda path, body: {"remaining_count": "1.50"})
    out = c.amend_resting("O1", "KXBTC15M-1", "yes", "buy", 0.26, 1.5)
    assert calls == [("POST", "/portfolio/events/orders/O1/amend", {"ticker": "KXBTC15M-1", "side": "bid", "price": "0.2600", "count": "1.50"}, True)]
    assert out["order_id"] == "O1" and out["remaining_count"] == "1.50"  # the id is filled in when the answer has none
    c.amend_resting("O2", "KXBTC15M-1", "no", "buy", 0.26, 1.5)  # a NO buy is an ask on the YES book at 1 - price
    assert calls[-1][2]["side"] == "ask" and calls[-1][2]["price"] == "0.7400" and "expiration_time" not in calls[-1][2]
    c.amend_resting("O3", "KXBTC15M-1", "yes", "sell", 0.61, 4.0, seconds=40)  # a YES sell is an ask; a new expiry when asked for
    assert calls[-1][2]["side"] == "ask" and calls[-1][2]["price"] == "0.6100" and calls[-1][2]["expiration_time"] > time.time() + 30
    c.amend_resting("O4", "KXBTC15M-1", "no", "sell", 0.61, 4.0)  # a NO sell is a bid on the YES book at 1 - price
    assert calls[-1][2]["side"] == "bid" and calls[-1][2]["price"] == "0.3900"


class Amends(Socket):
    def __init__(self, asks, account=None, refuse=False):
        super().__init__(asks, account)
        self.amended, self.refuse = [], refuse

    def amend_resting(self, order_id, ticker, side, action, price, count, seconds=None):
        if self.refuse:
            raise KalshiError("order not found")
        self.amended.append((order_id, side, action, round(price, 4), count))
        return {"order_id": order_id}


def test_a_fall_in_the_ask_amends_the_order_where_it_stands(setup):
    s, store, shot, spec = setup
    spec(buy_rest_seconds=60.0, buy_retries=0, buy_rest_follow=True)
    client = Amends(asks=[0.31, 0.27], account={"r1": 1.5})
    feed()
    th, out = run_place(s, client, store, shot)
    assert wait_for(lambda: restbuys._by_order.get("r1") is not None)
    restbuys.on_event({"kind": "quote", "ticker": shot.ticker, "bid": 0.25, "ask": 0.27, "ts": time.time()})
    assert wait_for(lambda: client.amended)
    assert client.amended == [("r1", "yes", "buy", 0.26, 1.5)]  # one cent under the new ask, the order's whole count
    assert client.cancels == [] and len(client.rested) == 1  # nothing cancelled, nothing placed again
    restbuys.on_event({"kind": "fill", "ticker": shot.ticker, "order_id": "r1", "count": 1.5, "action": "buy"})
    th.join(3.0)
    assert out["got"].filled == 1.5


def test_a_refused_amend_falls_back_to_cancel_and_place(setup):
    s, store, shot, spec = setup
    spec(buy_rest_seconds=60.0, buy_retries=0, buy_rest_follow=True)
    client = Amends(asks=[0.31, 0.27], account={"r2": 1.5}, refuse=True)
    feed()
    th, out = run_place(s, client, store, shot)
    assert wait_for(lambda: restbuys._by_order.get("r1") is not None)
    restbuys.on_event({"kind": "quote", "ticker": shot.ticker, "bid": 0.25, "ask": 0.27, "ts": time.time()})
    assert wait_for(lambda: len(client.rested) == 2)
    assert client.cancels == ["r1"] and client.rested[1][1] == 0.26  # the old way, as before
    restbuys.on_event({"kind": "fill", "ticker": shot.ticker, "order_id": "r2", "count": 1.5, "action": "buy"})
    th.join(3.0)
    assert out["got"].filled == 1.5


class AmendBook(Book):
    def __init__(self, refuse_amend=False):
        super().__init__()
        self.amended, self.refuse_amend = [], refuse_amend

    def amend_resting(self, order_id, ticker, side, action, price, count, seconds=None):
        if self.refuse_amend:
            raise KalshiError("order not found")
        self.amended.append((order_id, ticker, side, action, price, count, round(seconds)))
        return {"order_id": order_id}


def test_a_reprice_of_the_resting_sell_amends_the_order_with_a_fresh_expiry():
    b = AmendBook()
    r = manualrest.ManualRest(b)
    now = time.time()
    r.step(TK, "yes", 4.0, "rest", 0.65, 600, True, now)
    note = r.step(TK, "yes", 4.0, "rest", 0.61, 585, True, now + 15)
    assert "amended to 0.61" in note and "was 0.65" in note
    assert b.amended == [("O1", TK, "yes", "sell", 0.61, 4.0, 40)] and b.cancelled == [] and len(b.placed) == 1
    assert r.working[TK].order_id == "O1" and r.working[TK].price == pytest.approx(0.61)


def test_a_refused_amend_of_the_sale_cancels_and_places_the_new_ask():
    b = AmendBook(refuse_amend=True)
    r = manualrest.ManualRest(b)
    now = time.time()
    r.step(TK, "yes", 4.0, "rest", 0.65, 600, True, now)
    note = r.step(TK, "yes", 4.0, "rest", 0.61, 585, True, now + 15)
    assert "was 0.65" in note and b.cancelled == ["O1"] and b.placed[-1][3] == 0.61 and r.working[TK].order_id == "O2"


def test_the_followed_buy_still_rests_where_the_runner_says():
    assert runner.REST_FOLLOW_MAX == 20  # the cap on moves stays (each is a request, whichever call makes it); raised from 3 on 2026-11-24, when the order began to be re-judged on every quote and moved both ways


# ---- the resting orders go out on the tick grid (2026-11-23): a live GOLD NO buy resting at 0.894 went out as a YES ask of 0.106, off the cent grid of the 10c to 90c zone of the tapered ladder, and Kalshi refused it with invalid_price ----
def test_unit_a_book_price_never_gets_worse_than_the_price_meant():
    from kalshi_moonshot.kalshi import book_price

    assert book_price(0.106, "ask") == 0.11 and book_price(0.106, "bid") == 0.10  # a NO buy at 0.894 is an ask at 0.106: up to 0.11 = NO 0.89, never over the 0.894 meant
    assert book_price(0.143, "bid") == 0.14 and book_price(0.143, "ask") == 0.15  # a YES buy at 0.143 down to 0.14
    assert book_price(0.093, "bid") == book_price(0.093, "ask") == 0.093  # under 10c a tenth of a cent is the step: on the grid already
    assert book_price(0.5, "bid") == book_price(0.5, "ask") == 0.5


def test_integration_the_resting_buy_sale_and_amend_bodies_are_on_the_grid_for_both_sides():
    c, calls = client_with(lambda path, body: {"order_id": "O", "remaining_count": "1.00"})
    c.buy_resting("KXGOLD15M-1", "no", 6.0, 0.894, 5.0)  # the refused one
    assert calls[-1][2]["side"] == "ask" and calls[-1][2]["price"] == "0.1100"  # YES ask 0.11 = the NO buy at 0.89, not over the 0.894 meant
    c.buy_resting("KXGOLD15M-1", "yes", 6.0, 0.143, 5.0)
    assert calls[-1][2]["side"] == "bid" and calls[-1][2]["price"] == "0.1400"
    c.sell_resting("KXGOLD15M-1", "no", 6.0, 0.896, 5.0)  # a NO sale is a YES bid at 0.104: down to 0.10 = NO 0.90, never under the 0.896 asked
    assert calls[-1][2]["side"] == "bid" and calls[-1][2]["price"] == "0.1000"
    c.sell_resting("KXGOLD15M-1", "yes", 6.0, 0.613, 5.0)  # a YES sale at 0.613 up to 0.62
    assert calls[-1][2]["side"] == "ask" and calls[-1][2]["price"] == "0.6200"
    c.amend_resting("O1", "KXGOLD15M-1", "no", "buy", 0.894, 6.0)
    assert calls[-1][2]["side"] == "ask" and calls[-1][2]["price"] == "0.1100"
    c.amend_resting("O2", "KXGOLD15M-1", "no", "sell", 0.896, 6.0)
    assert calls[-1][2]["side"] == "bid" and calls[-1][2]["price"] == "0.1000"
    c.buy_resting("KXBTC15M-1", "no", 6.0, 0.26, 5.0)  # a price already on the grid does not move
    assert calls[-1][2]["price"] == "0.7400"
