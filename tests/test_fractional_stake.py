import pytest

from kalshi_moonshot import config, runner
from kalshi_moonshot.kalshi import KalshiError
from kalshi_moonshot.sizing import size_bet
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot, contracts_for_budget, order_fee


def test_a_fractional_budget_spends_the_stake_to_the_cent_where_whole_contracts_leave_a_gap():
    for ask in (0.617, 0.73, 0.88):
        whole = contracts_for_budget(2.0, ask)
        frac = contracts_for_budget(2.0, ask, fractional=True)
        spent = ask * frac + order_fee(ask, frac)
        assert spent <= 2.0 + 1e-9 and spent > 2.0 - 0.03 and frac >= whole
        assert round(frac, 2) == frac


def test_the_old_whole_contract_sizing_is_unchanged(tmp_path, monkeypatch):
    assert contracts_for_budget(2.0, 0.73) == 2 and contracts_for_budget(2.0, 0.63) == 3


def test_a_fixed_stake_lane_sizes_the_stake_in_fractional_contracts(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    from kalshi_moonshot import lanes

    spec = lanes.spec_for(s, "moonshot")
    spec = type(spec)(**{**spec.__dict__, "fixed_stake": 2.0, "max_bet_pct": 1.0, "max_exposure_pct": 1.0})
    got = size_bet(spec, s, 0.8, 0.73, available=100, equity=100, lane_exposure=0)
    assert got.contracts == pytest.approx(2.69, abs=0.011) and got.cost_usd > 1.97
    whole = size_bet(spec, s, 0.8, 0.73, available=100, equity=100, lane_exposure=0, fractional=False)
    assert whole.contracts == 2 and whole.cost_usd < 1.5


class Client:
    def __init__(self, refuse_fraction=True, error="invalid_count"):
        self.sent, self.refuse_fraction, self.error = [], refuse_fraction, error

    def orderbook(self, ticker):
        return {"orderbook_fp": {"no_dollars": [["0.2000", "100.00"]]}}

    def buy_yes_ioc(self, ticker, count, price, client_order_id=None):
        self.sent.append(count)
        if self.refuse_fraction and count != int(count):
            raise KalshiError(f'POST /portfolio/events/orders -> 400: {{"error":{{"code":"{self.error}"}}}}')
        return {"fill_count": count, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "o1"}

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
    shot = LongShot("KXBTC15M-1", "t", "r", 0.79, 0.80, 100.0, "2099-01-01T00:00:00Z", 0.2, lane="x", series="KXBTC15M", event_ticker="KXBTC15M-E", side="yes")
    yield s, Store(s.db_path), shot
    runner._NO_FRACTION.clear()


def test_a_fractional_order_that_fills_is_recorded_as_sent(setup):
    s, store, shot = setup
    client = Client(refuse_fraction=False)
    got = runner.place(s, client, store, shot, "why", 2.49)
    assert client.sent == [2.49] and got.filled == 2.49 and not runner._NO_FRACTION
    assert store.net_contracts("KXBTC15M-1", s.env) == pytest.approx(2.49)


def test_a_refused_fraction_is_retried_as_whole_contracts_and_fractions_are_dropped_after(setup):
    s, store, shot = setup
    client = Client()
    got = runner.place(s, client, store, shot, "why", 2.49)
    assert client.sent == [2.49, 2] and got.filled == 2 and runner._NO_FRACTION


def test_an_unfunded_account_is_not_mistaken_for_a_refused_fraction(setup):
    s, store, shot = setup
    client = Client(error="insufficient_balance")
    got = runner.place(s, client, store, shot, "why", 2.49)
    assert client.sent == [2.49] and got.filled == 0 and not runner._NO_FRACTION


def test_selling_a_fractional_live_position_sells_all_of_it_and_marks_it_exited(setup, monkeypatch):
    s, store, shot = setup
    monkeypatch.setenv("LIVE_LANES", "x")
    monkeypatch.setenv("COMMODITY_15M_LIVE", "true")
    s = config.load()
    store.add_order(ticker="KXBTC15M-1", env=s.env, count=2.49, price=0.8, fill_count=2.49, cost_usd=2.0, fee_usd=0.0, order_id="o1", contract="yes")
    store.add_decision(ticker="KXBTC15M-1", title="t", bid=0.79, ask=0.8, mode="trade", close_time="2099-01-01T00:00:00Z", lane="x", action="bet", contracts=2.49, stake_usd=2.0, side="yes")

    class Seller(Client):
        def sell_yes_ioc(self, ticker, count, price, client_order_id=None):
            self.sent.append(count)
            return {"fill_count": count, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "o2"}

    seller = Seller()
    out = runner.exit_position(s, seller, store, store.open_bets()[0], 0.7, "test")
    assert seller.sent == [2.49] and out.startswith("sold 2.49/2.49") and store.open_bets() == []


def test_an_older_postgres_table_with_whole_number_counts_is_widened(setup):
    from kalshi_moonshot import db

    s, store, _ = setup
    if not db.is_pg(store.db):
        pytest.skip("only Postgres has fixed integer columns")
    store.db.execute("ALTER TABLE decisions ALTER COLUMN contracts TYPE BIGINT")
    store.db.execute("ALTER TABLE orders ALTER COLUMN count TYPE BIGINT")
    store.db.commit()
    store._widen_counts()
    store.add_order(ticker="T", env=s.env, count=2.49, price=0.8, fill_count=2.49, cost_usd=2.0, fee_usd=0.0, order_id="o", contract="yes")
    store.add_decision(ticker="T", title="t", bid=0.7, ask=0.8, mode="observe", close_time="2099-01-01T00:00:00Z", lane="x", action="bet", contracts=2.49, stake_usd=2.0, side="yes")
    assert store.db.execute("SELECT contracts FROM decisions WHERE ticker='T'").fetchone()[0] == pytest.approx(2.49)
    assert store.net_contracts("T", s.env) == pytest.approx(2.49)
