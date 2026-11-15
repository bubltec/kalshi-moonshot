"""A profit take is sent once, at the bid, and never under what the bet cost (2026-11-14/15).
Live, the curve's profit take got the crash exit's retry ladder: a take decided at 0.88 sold 16 contracts at 0.871 and its other 20.73 at 0.860 (02:42 UTC). Its reason, "curve risk exit (profit take)", was also on the watcher's list of exits that may sell for a loss.
Unit: which reasons are profit sales. Integration: lane file -> refresh -> quote events -> profit signal -> order, with a partial fill, and with the bid gone under the cost before the order."""

from kalshi_moonshot import config, runner
from kalshi_moonshot.store import Store
from tests.test_risk_exit_flow import TICKER, Client, _clean_mids, climb, held, level  # noqa: F401


class Book(Client):
    """Fills only part of a sale; has a bid on the book a retry could chase."""

    def orderbook(self, ticker):
        return {"orderbook_fp": {"yes_dollars": [["0.5000", "10"]], "no_dollars": []}}


def test_unit_only_the_gain_taking_sales_are_profit_sales():
    for why in ("take-profit: bid 0.88 is 1.1x the 0.80 paid", "curve risk exit (profit take): the bet is 0.1 ahead", "curve risk exit (profit take): x [socket]"):
        assert runner.is_profit_sale(why)
    for why in ("curve risk exit (max loss): down", "curve risk exit: the line", "stop-loss: x", "peak-crash: y", "burst: z"):
        assert not runner.is_profit_sale(why)


def taken(tmp_path, monkeypatch, client):
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": True)
    w = held(tmp_path, monkeypatch, live=True, take="true", client=client)
    assert climb(w, 0.82, 0.92, 700) is None
    sig = level(w, 0.92, 700)
    assert sig is not None and sig.why.startswith("curve risk exit (profit take)")
    return w, sig


def test_integration_a_profit_take_that_fills_part_is_sent_once_and_the_rest_waits(tmp_path, monkeypatch):
    c = Book(bid=0.92, fills=2.0)
    w, sig = taken(tmp_path, monkeypatch, c)
    w._sell(*sig)
    assert len(c.sold) == 1, c.sold  # the old code sold the rest 2, 5 and 10 cents under a fresh read of the bid
    _, limit = c.sold[0]
    assert limit >= 0.90 and limit <= 0.92  # at the bid less the normal two cents
    assert len(Store(config.load().db_path).open_bets()) == 1  # the rest stays open for the next quote


def test_integration_a_profit_take_is_not_sent_when_the_bid_has_gone_under_the_cost(tmp_path, monkeypatch):
    c = Book(bid=0.92)
    w, sig = taken(tmp_path, monkeypatch, c)
    c.bid = 0.79  # by the time the order is read the bid is under the 0.81 the bet cost with its fee
    w._sell(*sig)
    assert c.sold == []
    assert len(Store(config.load().db_path).open_bets()) == 1


def test_unit_the_limit_of_a_profit_sale_never_goes_under_the_cost(tmp_path, monkeypatch):
    c = Book(bid=0.92)
    _, _ = taken(tmp_path, monkeypatch, c)
    s = config.load()
    store = Store(s.db_path)
    row = store.open_bets()[0]
    runner.exit_position(s, c, store, row, 0.835, "curve risk exit (profit take): x", slip=0.10)  # a wide slip would put the limit at 0.735
    assert c.sold and c.sold[-1][1] >= 0.81  # the buy filled at 0.789 and its fee brings the cost to 0.81


def test_unit_a_profit_sale_under_the_cost_sends_nothing(tmp_path, monkeypatch):
    c = Book(bid=0.92)
    _, _ = taken(tmp_path, monkeypatch, c)
    s = config.load()
    store = Store(s.db_path)
    line = runner.exit_position(s, c, store, store.open_bets()[0], 0.79, "curve risk exit (profit take): x")
    assert c.sold == [] and "not sent" in line


def test_a_crash_exit_still_retries_down_the_book(tmp_path, monkeypatch):
    c = Book(bid=0.56, fills=0.0)
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": True)
    held(tmp_path, monkeypatch, live=True, client=c)
    s = config.load()
    store = Store(s.db_path)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    runner.exit_position(s, c, store, store.open_bets()[0], 0.56, "curve risk exit (max loss): down")
    assert len(c.sold) >= 2
