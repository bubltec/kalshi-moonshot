"""The resting buy priced on the side's BID (lane field buy_rest_at_bid; 2026-11-23, user: "you should put the resting buy on the 'bid' value", "not the ask")."""
import time
from types import SimpleNamespace

from kalshi_moonshot import restprice, runner
from tests.test_buy_retry import setup  # noqa: F401  (the fixture)


def book(yes_bid, yes_ask):
    """A book with the YES bid and the YES ask (a NO bid at 1 - ask)."""
    return {"orderbook_fp": {"yes_dollars": [[f"{yes_bid:.4f}", "100.00"]], "no_dollars": [[f"{1.0 - yes_ask:.4f}", "100.00"]]}}


def test_the_price_helpers():
    assert restprice.side_bid(0.30, 0.35, "yes") == 0.30
    assert restprice.side_bid(0.30, 0.35, "no") == 0.65  # a NO bid is 1 minus the YES ask
    assert restprice.side_bid(0.0, 0.35, "yes") is None
    spec = SimpleNamespace(buy_rest_at_bid=True, buy_rest_below=0.02, buy_rest_avg_seconds=0.0)
    assert restprice.rest_price(spec, "yes", 0.40, [], 0.0, bid=0.33) == 0.33  # at the bid, not under the ask
    spec.buy_rest_at_bid = False
    assert restprice.rest_price(spec, "yes", 0.40, [], 0.0, bid=0.33) == 0.38  # off: under the ask as before


def test_integration_the_start_price_is_the_live_bid_and_never_the_ask(setup):
    _s, _store, shot, spec = setup
    sp = spec(buy_rest_seconds=5.0, buy_rest_below=0.01, buy_rest_at_bid=True)
    shot = type(shot)(shot.ticker, "t", "r", 0.30, 0.35, 100.0, "2099-01-01T00:00:00Z", 0.2, lane="scalpy", series="KXBTC15M", event_ticker="KXBTC15M-E", side="yes")
    price, why = runner._resting_price(shot, sp, book(0.30, 0.35))
    assert (price, why) == (0.30, "")  # not 0.34, which is 0.01 under the ask
    price, _ = runner._resting_price(shot, sp, book(0.28, 0.33))  # the bid has fallen since the decision
    assert price == 0.28
    sp2 = spec(buy_rest_seconds=5.0, buy_rest_below=0.01, buy_rest_at_bid=False)
    assert runner._resting_price(shot, sp2, book(0.30, 0.35))[0] == 0.34  # off: the ask less a cent


class Amend:
    def __init__(self, b):
        self.b, self.amends = b, []

    def orderbook(self, ticker):
        return self.b

    def amend_resting(self, order_id, ticker, side, action, price, count):
        self.amends.append((order_id, round(price, 4), count))
        return {"order_id": order_id}


def test_integration_the_order_follows_the_bid_down_and_up_while_the_module_buys(setup):
    s, _store, shot, spec = setup
    sp = spec(buy_rest_seconds=5.0, buy_rest_below=0.01, buy_rest_at_bid=True, buy_rest_follow=True, price=(0.02, 0.39))
    rest = SimpleNamespace(filled=0.0)
    c = Amend(book(0.28, 0.31))
    got = runner._rest_follow(s, c, shot, sp, 1.5, "k", "o1", 0.30, time.time() + 30, 0.0, 0, rest)
    assert (got.kind, got.order_id, got.price) == ("moved", "o1", 0.28) and c.amends == [("o1", 0.28, 1.5)]  # amended in place to the bid
    c2 = Amend(book(0.33, 0.35))
    got = runner._rest_follow(s, c2, shot, sp, 1.5, "k", "o1", 0.30, time.time() + 30, 0.0, 0, rest)
    assert got.price == 0.33 and c2.amends == [("o1", 0.33, 1.5)]  # since 2026-11-24 (restcheck.py) a risen bid raises the order too, while the lane's module still buys (it never did before)
    c3 = Amend(book(0.30, 0.32))
    assert runner._rest_follow(s, c3, shot, sp, 1.5, "k", "o1", 0.30, time.time() + 30, 0.0, 0, rest) is None and not c3.amends  # on the bid already: nothing sent


def test_the_rest_is_a_share_of_the_time_left_never_fixed_seconds(setup):
    _s, _store, shot, spec = setup
    sp = spec(buy_rest_seconds=5.0, buy_rest_wait_share=0.25, buy_rest_at_bid=True)
    assert restprice.rest_wait(sp, 800.0) == 200.0  # 0.25 of 800 s left
    assert restprice.rest_wait(sp, 60.0) == 15.0
    assert restprice.rest_wait(sp, 8.0) == 5.0  # never under buy_rest_seconds
    assert restprice.rest_wait(spec(buy_rest_seconds=5.0, buy_rest_wait_share=0.0), 800.0) == 5.0  # off: the fixed seconds as before
    from datetime import UTC, datetime, timedelta

    soon = (datetime.now(UTC) + timedelta(seconds=400)).isoformat()
    s2 = type(shot)(shot.ticker, "t", "r", 0.30, 0.35, 100.0, soon, 0.1, lane="scalpy", series="KXBTC15M", event_ticker="KXBTC15M-E", side="yes")
    assert 95.0 <= runner._rest_wait_for(s2, sp) <= 100.0  # 0.25 of the 400 s to the close


def test_gone_order_stops_following():
    from kalshi_moonshot import runner
    from kalshi_moonshot.kalshi import KalshiError

    assert runner._order_gone(KalshiError('POST /x/amend -> 404: {"error":{"code":"not_found"}}'))
    assert not runner._order_gone(KalshiError("POST /x -> 500: boom"))
