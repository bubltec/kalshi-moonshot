"""Two refinements of the profit sale (2026-11-15): its floor counts both fees, and a sale that misses is retried once at a fresh read of the bid, no slip.
Unit: the floor. Integration: lane file -> refresh -> quote events -> profit signal -> order, then the retry."""

import pytest

from kalshi_moonshot import config, runner
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract
from tests.test_profit_sale import taken
from tests.test_risk_exit_flow import Client, _clean_mids  # noqa: F401


@pytest.mark.parametrize("paid", [0.05, 0.30, 0.50, 0.789, 0.85, 0.94, 0.985])
def test_unit_the_floor_is_the_lowest_cent_that_clears_the_cost_after_both_fees(paid):
    f = runner.profit_floor(paid)
    need = paid + fee_per_contract(paid)
    assert f - fee_per_contract(f) >= need - 1e-9  # a sale at the floor is not a loss
    assert round(f - 0.01, 2) - fee_per_contract(round(f - 0.01, 2)) < need  # and no lower cent is enough


class Misses(Client):
    """Fills what `plan` says for each sale in turn (a fraction of the count), with a bid on the book a retry reads."""

    def __init__(self, plan, book_bid, **kw):
        super().__init__(**kw)
        self.plan, self.book_bid = list(plan), book_bid

    def orderbook(self, ticker):
        return {"orderbook_fp": {"yes_dollars": [[f"{self.book_bid:.4f}", "10"]], "no_dollars": []}}

    def sell_yes_ioc(self, ticker, count, price, client_order_id=None):
        self.sold.append((count, price))
        part = self.plan.pop(0) if self.plan else 0.0
        return {"fill_count": round(count * part, 2), "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "s"}


def test_integration_a_missed_profit_sale_is_retried_once_at_the_bid_with_no_slip(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    c = Misses([0.0, 1.0], book_bid=0.91, bid=0.92)
    w, sig = taken(tmp_path, monkeypatch, c)
    w._sell(*sig)
    assert len(c.sold) == 2 and c.sold[1][1] == 0.91  # the retry is at the fresh bid, not a cent under it
    assert len(Store(config.load().db_path).open_bets()) == 0


def test_integration_a_part_fill_sells_the_rest_once_at_the_bid_and_no_more(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    c = Misses([0.4, 0.0, 0.0], book_bid=0.91, bid=0.92)
    w, sig = taken(tmp_path, monkeypatch, c)
    w._sell(*sig)
    assert len(c.sold) == 2 and abs(c.sold[1][0] - 0.6 * c.sold[0][0]) < 0.02  # one retry, for what was left
    assert len(Store(config.load().db_path).open_bets()) == 1  # still open: the next quote decides


def test_integration_no_retry_when_the_fresh_bid_is_under_the_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    c = Misses([0.0, 1.0], book_bid=0.78, bid=0.92)
    w, sig = taken(tmp_path, monkeypatch, c)
    w._sell(*sig)
    assert len(c.sold) == 1  # the book fell under what the bet cost: the old ladder would have sold at 0.76, 0.73, 0.68
    assert len(Store(config.load().db_path).open_bets()) == 1


def test_integration_a_bid_between_the_entry_fee_floor_and_the_both_fee_floor_is_not_sold(tmp_path, monkeypatch):
    c = Misses([1.0, 1.0], book_bid=0.92, bid=0.92)
    _, _ = taken(tmp_path, monkeypatch, c)
    s = config.load()
    store = Store(s.db_path)
    paid = store.entry_fill("KXBTC15M-30JAN010015-15", "yes", 0.0) or 0.789
    both = runner.profit_floor(paid)
    old = -(-int((paid + fee_per_contract(paid)) * 100 - 1e-9) // 1) / 100  # the entry-fee-only floor of ec337f3, in cents (rounded up)
    assert both > round(old, 2) or both >= old
    line = runner.exit_position(s, c, store, store.open_bets()[0], round(both - 0.01, 2), "curve risk exit (profit take): x")
    assert c.sold == [] and "not sent" in line
    runner.exit_position(s, c, store, store.open_bets()[0], both, "curve risk exit (profit take): x", slip=0.10)
    assert c.sold and c.sold[0][1] >= both
