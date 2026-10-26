"""A resting take-profit sell: placed when a live buy fills, filled (if the bid reaches it) with no taker fee.

The order itself, the bookkeeping of its fills, and the truce with the exits that would otherwise cross the
book to sell the same contracts: the watcher's take-profit stands down while the order works, and a stop or
crash exit cancels the order and books its fills before selling what is left.
"""

import time

import pytest

from kalshi_moonshot import config, lanes, runner
from kalshi_moonshot.kalshi import KalshiClient, KalshiError
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot

CLOSE = "2099-01-01T00:00:00Z"


class Posted(KalshiClient):
    def __init__(self):
        super().__init__("https://example.invalid/trade-api/v2")
        self.sent: list[dict] = []

    def _request(self, method, path, *, params=None, json=None, auth=False):
        self.sent.append({"method": method, "path": path, "json": json})
        return {"order_id": "ORD1"}


def test_a_resting_yes_sell_is_a_post_only_reduce_only_ask_that_cancels_itself():
    c = Posted()
    before = time.time()
    c.sell_resting("KXT-1", "yes", 2, 0.52, 180)
    body = c.sent[0]["json"]
    assert c.sent[0]["path"] == "/portfolio/events/orders"
    assert (body["side"], body["price"], body["count"]) == ("ask", "0.5200", "2.00")
    assert body["time_in_force"] == "good_till_canceled" and body["post_only"] is True and body["reduce_only"] is True
    assert before + 179 <= body["expiration_time"] <= time.time() + 181


def test_a_resting_no_sell_is_a_bid_on_the_yes_book_at_one_minus_the_price():
    c = Posted()
    c.sell_resting("KXT-1", "no", 1, 0.47, 60)
    body = c.sent[0]["json"]
    assert (body["side"], body["price"], body["reduce_only"]) == ("bid", "0.5300", True)  # selling NO at 0.47 is bidding 0.53 for YES back


def test_cancelling_an_order_deletes_it_by_id():
    c = Posted()
    c.cancel_order("ORD9")
    assert c.sent[0]["method"] == "DELETE" and c.sent[0]["path"] == "/portfolio/events/orders/ORD9"


def _spec(**kw):
    base = {"name": "fav", "estimator": "ladder", "label": "50-90c", "price": (0.5, 0.9), "hours": (0.0, 1.0), "max_spread": 0.1, "min_volume": 0}
    return lanes.LaneSpec(**{**base, **kw})


def test_the_resting_price_is_the_lanes_own_target():
    assert runner.rest_target_price(_spec(take_profit_x=1.5), 0.40) == pytest.approx(0.60)
    assert runner.rest_target_price(_spec(take_profit_at=0.95), 0.40) == pytest.approx(0.95)
    assert runner.rest_target_price(_spec(take_profit_x=3.0, take_profit_at=0.95), 0.40) == pytest.approx(0.95)  # 3x of 0.40 is past $1: not a target
    assert runner.rest_target_price(_spec(), 0.40) is None
    assert runner.rest_target_price(_spec(take_profit_at=0.30), 0.40) is None  # already past it


def _fill(order_id, count, yes_price, fee="0", ts=2000.0, fill_id="f1"):
    return {"fill_id": fill_id, "market_ticker": "KXBTC-1", "count_fp": str(count), "yes_price_dollars": str(yes_price),
            "no_price_dollars": str(1 - yes_price), "fee_cost": fee, "ts": ts, "book_side": "ask",
            "is_taker": False, "order_id": order_id, "action": "sell", "side": "yes"}


class Exchange:
    """A fake live account: holds what the buy filled, rests the sell, fills it when told."""

    def __init__(self, fills=(), refuse=False):
        self.resting: list = []
        self.cancelled: list = []
        self.ioc_sold: list = []
        self.fills = list(fills)
        self.refuse = refuse

    def sell_resting(self, ticker, side, count, price, seconds, client_order_id=None, post_only=True):
        if self.refuse:
            raise KalshiError("post_only_order_would_cross: the bid is already at the price")
        self.resting.append((ticker, side, count, price, seconds))
        return {"order_id": "R1"}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {}

    def portfolio_rows(self, path, key, min_ts=None, pages=1):
        return list(self.fills)

    def sell_yes_ioc(self, ticker, count, price, client_order_id=None):
        self.ioc_sold.append((ticker, count, price))
        return {"fill_count": count, "average_fill_price": price, "average_fee_paid": 0.01, "order_id": "IOC1"}

    def market(self, ticker):
        return {"ticker": ticker, "yes_bid_dollars": "0.60", "yes_ask_dollars": "0.61", "close_time": CLOSE, "result": ""}


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODE", "trade")
    monkeypatch.setenv("LIVE_LANES", "fav")
    s = config.load()
    store = Store(s.db_path)
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1000.0, "KXBTC-1", "bet", "trade", 0.40, "yes", "fav", 5, 2.0, CLOSE))
    store.add_order(ticker="KXBTC-1", env=s.env, count=5, price=0.40, fill_count=5, cost_usd=2.0, fee_usd=0.05, order_id="B1", side="buy", contract="yes")
    store.db.commit()
    return s, store


def _shot():
    return LongShot("KXBTC-1", "t", "", 0.39, 0.40, 10.0, CLOSE, 1.0, lane="fav", series="KXBTC", side="yes")


def test_a_filled_buy_rests_its_take_profit_and_records_the_order(live):
    s, store = live
    ex = Exchange()
    note = runner._rest_take_profit(s, ex, store, _shot(), _spec(rest_sell=True, take_profit_x=1.5), 5, 0.40)
    ticker, side, count, price, seconds = ex.resting[0]
    assert (ticker, side, count, price) == ("KXBTC-1", "yes", 5, pytest.approx(0.60))
    assert seconds > 30  # good until the close, not a moment's order
    assert "rests at 0.60" in note and "no taker fee" in note
    working = store.working_rest_sell("KXBTC-1", s.env)
    assert working is not None and working["order_id"] == "R1" and float(working["price"]) == pytest.approx(0.60)


def test_no_resting_sell_without_the_knob_or_a_target(live):
    s, store = live
    ex = Exchange()
    assert runner._rest_take_profit(s, ex, store, _shot(), _spec(take_profit_x=1.5), 5, 0.40) == ""
    assert runner._rest_take_profit(s, ex, store, _shot(), _spec(rest_sell=True), 5, 0.40) == ""
    assert ex.resting == [] and store.working_rest_sell("KXBTC-1", s.env) is None


def test_a_refused_resting_sell_is_only_a_note(live):
    s, store = live
    ex = Exchange(refuse=True)
    note = runner._rest_take_profit(s, ex, store, _shot(), _spec(rest_sell=True, take_profit_x=1.5), 5, 0.40)
    assert "refused" in note and "watcher will sell at the target instead" in note
    assert store.working_rest_sell("KXBTC-1", s.env) is None


def _rest(s, store, ex):
    runner._rest_take_profit(s, ex, store, _shot(), _spec(rest_sell=True, take_profit_x=1.5), 5, 0.40)


def test_a_full_fill_is_booked_fee_free_and_closes_the_bet(live):
    s, store = live
    ex = Exchange(fills=[_fill("R1", 5, 0.60)])
    _rest(s, store, ex)
    row = store.open_bets()[0]
    line = runner.exit_position(s, ex, store, row, 0.60, "stop-loss: down hard")
    assert ex.cancelled == ["R1"] and ex.ioc_sold == []  # the resting order did it all; nothing crossed the book
    assert "resting @ 0.6000" in line and "no taker fee" in line
    assert store.net_contracts("KXBTC-1", s.env) == 0
    done = store.db.execute("SELECT exit_price, exit_reason FROM decisions WHERE ticker='KXBTC-1'").fetchone()
    assert done["exit_price"] == pytest.approx(0.60) and "no taker fee" in done["exit_reason"]
    sell = store.db.execute("SELECT fee_usd FROM orders WHERE side='sell'").fetchone()
    assert sell["fee_usd"] == 0.0  # the fee Kalshi charged a rested order: none, recorded as it came


def test_a_partial_fill_leaves_the_rest_to_the_immediate_sale(live):
    s, store = live
    ex = Exchange(fills=[_fill("R1", 2, 0.60)])
    _rest(s, store, ex)
    row = store.open_bets()[0]
    line = runner.exit_position(s, ex, store, row, 0.10, "stop-loss: down hard")
    assert ex.cancelled == ["R1"]
    assert ex.ioc_sold == [("KXBTC-1", 3, 0.08)]  # only the 3 the resting sell had not already sold
    assert "sold 3/3" in line and store.net_contracts("KXBTC-1", s.env) == 0
    assert store.working_rest_sell("KXBTC-1", s.env) is None  # retired: nothing keeps standing down for it


def test_the_watcher_books_a_fill_and_closes_the_bet(live):
    from kalshi_moonshot import watcher

    s, store = live
    ex = Exchange(fills=[_fill("R1", 5, 0.60)])
    _rest(s, store, ex)
    w = watcher.ProfitWatcher(lambda: s, ex, lambda: store)
    w._book_resting(store, s)
    assert store.open_bets() == [] and store.net_contracts("KXBTC-1", s.env) == 0
    assert w.sold and "resting @ 0.6000" in w.sold[-1] and "no taker fee" in w.sold[-1]


def test_the_watchers_take_profit_stands_down_while_a_resting_sell_works(live):
    from kalshi_moonshot import watcher

    s, store = live
    ex = Exchange()
    _rest(s, store, ex)
    w = watcher.ProfitWatcher(lambda: s, ex, lambda: store)
    w._sell("KXBTC-1", "fav", "take-profit: bid 0.60 is 1.5x the 0.40 paid")
    assert ex.ioc_sold == [] and ex.cancelled == [] and store.working_rest_sell("KXBTC-1", s.env) is not None


def _paper(tmp_path, monkeypatch, toml):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()  # observe mode: the lane is paper
    (s.data_dir / "strategies.toml").write_text(toml)
    store = Store(s.db_path)
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1000.0, "KXBTC-1", "bet", "observe", 0.80, "yes", "fav", 5, 2.0, CLOSE))
    store.db.commit()
    return s, store


def test_a_paper_take_profit_books_the_target_not_the_reread_bid(tmp_path, monkeypatch):
    # The bid touched 0.88 (1.1x the 0.80 paid) and slipped to 0.86 before the watcher re-read it. Live, the
    # resting sell filled at 0.88 at the touch; the paper sale must book 0.88, not the dipped re-read.
    s, store = _paper(tmp_path, monkeypatch, "[lane.fav]\nestimator='ladder'\nprice=[0.5,0.9]\nhours=[0.0,1.0]\ntake_profit_x=1.1\nrest_sell=true\n")
    line = runner.exit_position(s, object(), store, store.open_bets()[0], 0.86, "take-profit: bid 0.88 is 1.1x the 0.80 paid")
    assert "paper-sold KXBTC-1 @ 0.88" in line
    done = store.db.execute("SELECT exit_price FROM decisions WHERE ticker='KXBTC-1'").fetchone()
    assert done["exit_price"] == pytest.approx(0.88)


def test_a_paper_take_profit_without_a_resting_sell_books_the_bid(tmp_path, monkeypatch):
    # No resting sell: live this lane would cross the book at the re-read bid, so paper books the bid as before.
    s, store = _paper(tmp_path, monkeypatch, "[lane.fav]\nestimator='ladder'\nprice=[0.5,0.9]\nhours=[0.0,1.0]\ntake_profit_x=1.1\n")
    line = runner.exit_position(s, object(), store, store.open_bets()[0], 0.86, "take-profit: bid 0.88 is 1.1x the 0.80 paid")
    assert "paper-sold KXBTC-1 @ 0.86" in line


def test_the_periodic_pass_stands_down_while_a_resting_sell_works(live):
    s, store = live
    ex = Exchange()
    _rest(s, store, ex)
    (s.data_dir / "strategies.toml").write_text("[lane.fav]\nestimator='ladder'\nprice=[0.5,0.9]\nhours=[0.0,1.0]\ntake_profit_x=1.5\nrest_sell=true\n")
    notes = runner.stop_loss_pass(s, ex, store)
    assert notes == [] and ex.ioc_sold == []  # the bid (0.60) is at the target, but the resting order is on it
