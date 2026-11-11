"""The resting curve sell on hand-opened 15-minute positions (manualrest.py, MANUAL_REST_CURVE): an ask at the curve's
chance plus a margin that shrinks with the time left, repriced every 15 s, and a time-to-go sale at the bid late on."""

import time
from datetime import UTC, datetime

import pytest

from kalshi_moonshot import config, manualrest, variants, watcher
from kalshi_moonshot.kalshi import KalshiError
from kalshi_moonshot.store import Store

TK = "KXBTC15M-26NOV111000-00"


def test_the_ask_is_the_chance_plus_a_margin_that_shrinks_with_the_time_left():
    assert manualrest.plan(0.50, 900, 0.40, 0.15, 120) == ("rest", 0.65)
    assert manualrest.plan(0.50, 225, 0.40, 0.15, 120) == ("rest", 0.58)  # half the margin with a quarter of the time left: 0.575, up to the cent
    assert manualrest.plan(0.95, 600, 0.90, 0.15, 120) == ("rest", 0.99)


def test_the_ask_never_sits_at_or_under_the_bid():
    assert manualrest.plan(0.50, 225, 0.70, 0.15, 120) == ("rest", 0.71)


def test_the_guard_sells_late_only_when_the_chance_is_under_one_half():
    assert manualrest.plan(0.30, 120, 0.25, 0.15, 120)[0] == "go"
    assert manualrest.plan(0.30, 121, 0.25, 0.15, 120)[0] == "rest"
    assert manualrest.plan(0.55, 60, 0.50, 0.15, 120)[0] == "rest"
    assert manualrest.plan(0.30, 60, 0.25, 0.15, 0)[0] == "rest"  # no guard set
    assert manualrest.plan(0.30, 60, 0.0, 0.15, 120)[0] == "rest"  # no bid to sell to


def test_nothing_happens_without_a_reading_in_the_last_seconds_or_when_off():
    assert manualrest.plan(None, 300, 0.4, 0.15, 120)[0] == "hold"
    assert manualrest.plan(0.8, 5, 0.7, 0.15, 120)[0] == "hold"
    assert manualrest.plan(0.8, 300, 0.7, 0.0, 120)[0] == "hold"
    assert manualrest.plan(0.8, 1200, 0.7, 0.15, 120)[0] == "hold"


class Book:
    def __init__(self, refuse=False):
        self.placed, self.cancelled, self.refuse = [], [], refuse

    def sell_resting(self, ticker, side, count, price, seconds):
        if self.refuse:
            raise KalshiError("post only cross")
        self.placed.append((ticker, side, count, price, round(seconds)))
        return {"order_id": f"O{len(self.placed)}"}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {}


def test_an_ask_is_placed_then_left_alone_until_the_next_reprice():
    b = Book()
    r = manualrest.ManualRest(b)
    now = time.time()
    assert "rests at 0.65" in r.step(TK, "yes", 4.0, "rest", 0.65, 600, True, now)
    assert r.step(TK, "yes", 4.0, "rest", 0.60, 590, True, now + 10) == ""  # under 15 s: the ask stands
    assert b.placed == [(TK, "yes", 4.0, 0.65, 40)] and b.cancelled == []


def test_a_reprice_cancels_the_old_ask_and_places_the_new_one():
    b = Book()
    r = manualrest.ManualRest(b)
    now = time.time()
    r.step(TK, "yes", 4.0, "rest", 0.65, 600, True, now)
    note = r.step(TK, "yes", 4.0, "rest", 0.61, 585, True, now + 15)
    assert "0.61" in note and "was 0.65" in note
    assert b.cancelled == ["O1"] and b.placed[-1][3] == 0.61 and r.working[TK].order_id == "O2"


def test_an_unchanged_price_keeps_the_order_until_its_life_runs_short():
    b = Book()
    r = manualrest.ManualRest(b)
    now = time.time()
    r.step(TK, "no", 2.0, "rest", 0.65, 600, True, now)
    assert r.step(TK, "no", 2.0, "rest", 0.65, 585, True, now + 15) == "" and len(b.placed) == 1
    assert r.step(TK, "no", 2.0, "rest", 0.65, 570, True, now + 30) != "" and len(b.placed) == 2  # 10 s of life left: replaced


def test_a_refused_ask_leaves_nothing_working_and_the_guard_withdraws_the_ask():
    r = manualrest.ManualRest(Book(refuse=True))
    assert "refused" in r.step(TK, "yes", 4.0, "rest", 0.65, 600, True) and TK not in r.working
    b = Book()
    r = manualrest.ManualRest(b)
    r.step(TK, "yes", 4.0, "rest", 0.65, 600, True)
    assert "withdrawn" in r.step(TK, "yes", 4.0, "go", 0.0, 100, True)
    assert b.cancelled == ["O1"] and TK not in r.working


def test_with_trading_off_the_ask_is_only_logged_and_only_when_it_moves():
    b = Book()
    r = manualrest.ManualRest(b)
    assert "would rest" in r.step(TK, "yes", 4.0, "rest", 0.65, 600, False)
    assert r.step(TK, "yes", 4.0, "rest", 0.65, 590, False) == ""
    assert b.placed == []


def test_an_order_whose_position_is_gone_is_forgotten():
    b = Book()
    r = manualrest.ManualRest(b)
    r.step(TK, "yes", 4.0, "rest", 0.65, 600, True)
    assert r.forget_gone({"OTHER"}) == [TK] and r.working == {} and b.cancelled == []


class Client(Book):
    authed = True

    def __init__(self, left=600.0, bid=0.40, rows=None):
        super().__init__()
        self.close = datetime.fromtimestamp(time.time() + left, UTC).isoformat()
        self.bid, self.sold = bid, []
        self.rows = rows if rows is not None else [{"ticker": TK, "position_fp": "4.00", "market_exposure_dollars": "1.60"}]

    def positions(self):
        return {"market_positions": self.rows}

    def market(self, ticker):
        return {"ticker": ticker, "yes_bid_dollars": str(self.bid), "yes_ask_dollars": str(self.bid + 0.01), "close_time": self.close}

    def sell_yes_ioc(self, ticker, count, price):
        self.sold.append((ticker, count, price))
        return {"fill_count": count, "average_fill_price": price}


@pytest.fixture
def s(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MANUAL_EXIT", "true")
    monkeypatch.setenv("MANUAL_REST_CURVE", "0.15")
    monkeypatch.setenv("MODE", "trade")
    monkeypatch.setenv("KALSHI_ENV", "demo")
    return config.load()


def _watcher(s, c, monkeypatch, chance):
    monkeypatch.setattr(variants, "curve_edge_now", lambda *a, **k: None if chance is None else (chance, 0.0))
    w = watcher.ProfitWatcher(lambda: s, c, lambda: Store(s.db_path))
    w.refresh()
    return w


def test_the_watcher_rests_the_curve_ask_on_a_hand_opened_position(s, monkeypatch):
    c = Client(left=600.0)
    w = _watcher(s, c, monkeypatch, 0.50)
    assert len(c.placed) == 1 and c.placed[0][:3] == (TK, "yes", 4.0)
    assert c.placed[0][3] == pytest.approx(0.63)  # 0.50 + 0.15 * sqrt(600 / 900) = 0.6225, up to the cent
    assert w.targets[(TK, "manual")].rest_rule
    w.refresh()
    assert len(c.placed) == 1  # the same cycle of 15 s: nothing new


def test_the_rule_is_off_unless_asked_for(s, monkeypatch):
    monkeypatch.setenv("MANUAL_REST_CURVE", "0")
    s2 = config.load()
    c = Client()
    w = _watcher(s2, c, monkeypatch, 0.50)
    assert c.placed == [] and not w.targets[(TK, "manual")].rest_rule


def test_a_position_with_no_curve_reading_keeps_the_older_rules(s, monkeypatch):
    c = Client()
    w = _watcher(s, c, monkeypatch, None)
    assert c.placed == [] and not w.targets[(TK, "manual")].rest_rule


def test_a_market_that_is_not_fifteen_minutes_is_left_alone(s, monkeypatch):
    c = Client(rows=[{"ticker": "KXWTI-26NOV1114-T98.99", "position_fp": "4.00", "market_exposure_dollars": "1.60"}])
    _watcher(s, c, monkeypatch, 0.50)
    assert c.placed == []


def test_the_older_profit_take_stands_down_while_the_ask_works(s, monkeypatch):
    c = Client(left=600.0, bid=0.60)
    w = _watcher(s, c, monkeypatch, 0.50)
    w._sell_manual(s, Store(s.db_path), TK, "crash: the bid fell 0.05 from its peak")
    assert c.sold == [] and c.cancelled == []


def test_late_and_behind_the_position_is_sold_at_the_bid_under_its_cost(s, monkeypatch):
    c = Client(left=100.0, bid=0.20)
    w = _watcher(s, c, monkeypatch, 0.30)
    t = w.targets[(TK, "manual")]
    t.last_bid, t.last_ts = 0.20, time.time()
    w.refresh()
    sig = w._jobs.get_nowait()
    assert sig.lane == "manual" and sig.why.startswith(manualrest.GO_PREFIX)
    w._sell_manual(s, Store(s.db_path), TK, sig.why)
    assert len(c.sold) == 1 and c.sold[0][1] == 4.0 and c.sold[0][2] < 0.40  # paid 0.40: sold under cost
    assert len(c.placed) == 1 and c.cancelled == ["O1"]  # the first cycle had no bid yet and rested an ask; the guard took it off the book before selling
