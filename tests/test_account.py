import pytest

from kalshi_moonshot import account, trips
from kalshi_moonshot.kalshi import KalshiError
from kalshi_moonshot.store import Store


def fill(fid, ts, ticker, side, count, yes, order="o", fee=0.0):
    return {"fill_id": fid, "ts": ts, "market_ticker": ticker, "book_side": side, "count_fp": str(count),
            "yes_price_dollars": str(yes), "no_price_dollars": str(round(1 - yes, 4)), "fee_cost": str(fee),
            "is_taker": True, "order_id": order, "action": "buy", "side": "yes"}


def settle(ticker, result, yes=0.0, no=0.0, fee=0.0, when="2026-10-01T12:00:00+00:00"):
    return {"ticker": ticker, "settled_time": when, "market_result": result, "yes_count_fp": str(yes),
            "no_count_fp": str(no), "fee_cost": str(fee)}


class FakeClient:
    def __init__(self, fills=(), sets=()):
        self.fills, self.sets = list(fills), list(sets)
        self.calls: list[tuple] = []
        self.fail: set[str] = set()

    def portfolio_rows(self, path, key, min_ts=None, pages=60):
        self.calls.append((path, min_ts))
        yield from (self.fills if key == "fills" else self.sets)

    def market(self, ticker):
        if ticker in self.fail:
            raise KalshiError("nope")
        return {"open_time": "2026-10-01T11:45:00+00:00", "close_time": "2026-10-01T12:00:00+00:00",
                "result": "yes", "floor_strike": 100.0}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def test_bad_rows_are_dropped():
    assert account.fill_row({"fill_id": "x"}) is None
    assert account.fill_row(fill("a", 1, "T-1", "weird", 1, 0.5)) is None
    assert account.settlement_row({"ticker": "T"}) is None


def test_sync_is_idempotent_and_overlaps(store):
    c = FakeClient([fill("a", 1000, "KXBTC15M-1", "bid", 2, 0.4)], [settle("KXBTC15M-1", "yes", yes=2)])
    first = account.sync(c, store)
    assert first["fills"] == 1 and first["settlements"] == 1 and first["markets"] == 1
    again = account.sync(c, store)
    assert again["fills"] == 0 and again["settlements"] == 0 and again["markets"] == 0
    assert c.calls[2][1] == 1000 - account.OVERLAP  # the second run only asks for recent rows


def test_market_lookups_are_budgeted_and_failures_retried(store):
    c = FakeClient([fill("a", 1, "A-1", "bid", 1, 0.5), fill("b", 2, "B-1", "bid", 1, 0.5)])
    c.fail = {"B-1"}
    r = account.sync(c, store, market_budget=5)
    assert r["markets"] == 1 and r["markets_left"] == 1
    c.fail = set()
    assert account.sync(c, store)["markets"] == 1


def test_buy_then_cash_out(store):
    c = FakeClient([fill("a", 1000, "KXBTC15M-1", "bid", 10, 0.20, order="m1"),
                    fill("b", 1300, "KXBTC15M-1", "ask", 10, 0.80, order="m2")])
    account.sync(c, store)
    [t] = trips.build_trips(store)
    assert t.done and t.exit_type == "cash-out" and t.source == "manual"
    assert t.pnl == pytest.approx(6.0)
    assert trips.cashout_multiples([t]) == [pytest.approx(4.0)]


def test_buying_no_then_settling(store):
    c = FakeClient([fill("a", 1000, "KXBTC15M-1", "ask", 5, 0.70)], [settle("KXBTC15M-1", "no", no=5, fee=0.1)])
    account.sync(c, store)
    [t] = trips.build_trips(store)
    assert t.side == "no" and t.exit_type == "settlement" and t.reliable
    assert t.cost == pytest.approx(1.5)
    assert t.pnl == pytest.approx(5 * 0.70 - 0.1)


def test_partial_close_settles_the_rest(store):
    c = FakeClient([fill("a", 1000, "X-1", "bid", 10, 0.30), fill("b", 1100, "X-1", "ask", 4, 0.60)],
                   [settle("X-1", "no", yes=6)])
    account.sync(c, store)
    [t] = trips.build_trips(store)
    assert t.exit_type == "mixed"
    assert t.pnl == pytest.approx(4 * 0.30 + 6 * -0.30)


def test_a_position_that_disagrees_with_the_settlement_is_unreliable(store):
    c = FakeClient([fill("a", 1000, "X-1", "bid", 10, 0.30)], [settle("X-1", "yes", yes=25)])
    account.sync(c, store)
    [t] = trips.build_trips(store)
    assert not t.reliable and trips.finished([t]) == []


def test_source_comes_from_the_bots_own_orders(store):
    store.db.execute("INSERT INTO orders (ts, ticker, env, count, price, fill_count, cost_usd, fee_usd, order_id)"
                     " VALUES (1, 'X-1', 'demo', 1, 0.5, 1, 0.5, 0, 'bot1')")
    store.db.commit()
    both = FakeClient([fill("a", 1000, "X-1", "bid", 1, 0.5, order="bot1"),
                       fill("b", 1100, "X-1", "ask", 1, 0.6, order="hand")])
    account.sync(both, store)
    assert trips.build_trips(store)[0].source == "mixed"
    only_bot = FakeClient([fill("c", 2000, "Y-1", "bid", 1, 0.5, order="bot1"),
                           fill("d", 2100, "Y-1", "ask", 1, 0.6, order="bot1")])
    account.sync(only_bot, store)
    assert {t.ticker: t.source for t in trips.build_trips(store)}["Y-1"] == "bot"


def test_open_trips_are_not_finished(store):
    account.sync(FakeClient([fill("a", 1000, "X-1", "bid", 1, 0.5)]), store)
    [t] = trips.build_trips(store)
    assert not t.done and trips.finished([t]) == []


def test_fifteen_minute_entry_minute(store):
    c = FakeClient([fill("a", 1, "KXBTC15M-1", "bid", 1, 0.5)])
    account.sync(c, store)
    [t] = trips.build_trips(store)
    t.opened_ts = t.close_ts - 5 * 60
    assert t.minute_in_window(t.opened_ts) == pytest.approx(10.0)
