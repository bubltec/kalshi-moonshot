
from kalshi_moonshot import config, runner
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot


class Client:
    def __init__(self, cash):
        self.cash = dict(cash)
        self.moves = []
        self.orders = 0
        self.authed = True

    def market(self, ticker):
        return {"exchange_index": 0}

    def shard_balances(self):
        return dict(self.cash)

    def shard_transfer(self, usd, source, src, destination, dst):
        self.moves.append((usd, src, dst))
        self.cash[src] -= usd
        self.cash[dst] += usd
        return {"transfer_id": "t"}

    def orderbook(self, ticker):
        return {"orderbook_fp": {"no_dollars": [["0.7000", "100.00"]]}}

    def buy_yes_ioc(self, ticker, count, price, client_order_id=None):
        self.orders += 1
        return {"fill_count": count, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "o"}


def make(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODE", "trade")
    monkeypatch.setenv("KALSHI_ENV", "demo")
    monkeypatch.setenv("MIN_ORDER_USD", "0.25")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    runner._EXCHANGE_OF.clear()
    runner._UNFUNDED.clear()
    monkeypatch.setattr(runner.time, "sleep", lambda x: None)
    s = config.load()
    shot = LongShot("KXEURUSD15M-1", "t", "r", 0.29, 0.30, 100.0, "2099-01-01T00:00:00Z", 0.2,
                    lane="fx_ticket", series="KXEURUSD15M", event_ticker="KXEURUSD15M-E", side="yes")
    return s, Store(s.db_path), shot


def test_off_by_default_moves_nothing(tmp_path, monkeypatch):
    s, store, shot = make(tmp_path, monkeypatch)
    c = Client({0: 0.0, 2: 100.0})
    assert runner._top_up(s, c, store, shot, 0.9) is None and c.moves == []


def test_a_short_exchange_is_topped_up_to_the_target_from_the_richest(tmp_path, monkeypatch):
    s, store, shot = make(tmp_path, monkeypatch, SHARD_TOPUP_TARGET_USD="10")
    c = Client({0: 0.0, 1: 5.0, 2: 100.0})
    assert runner._top_up(s, c, store, shot, 0.9) is None
    assert c.moves == [(10.0, 2, 0)] and c.cash[0] == 10.0
    assert store.shard_moved_since(0) == 10.0


def test_no_move_when_the_exchange_already_has_enough(tmp_path, monkeypatch):
    s, store, shot = make(tmp_path, monkeypatch, SHARD_TOPUP_TARGET_USD="10")
    c = Client({0: 3.0, 2: 100.0})
    assert runner._top_up(s, c, store, shot, 0.9) is None and c.moves == []


def test_the_source_keeps_its_floor(tmp_path, monkeypatch):
    s, store, shot = make(tmp_path, monkeypatch, SHARD_TOPUP_TARGET_USD="10", SHARD_TOPUP_KEEP_USD="95")
    c = Client({0: 0.0, 2: 100.0})  # only $5 above the floor: enough for the order, less than the target
    assert runner._top_up(s, c, store, shot, 0.9) is None
    assert c.moves == [(5.0, 2, 0)] and c.cash[2] == 95.0
    c2 = Client({0: 0.0, 2: 95.5})  # $0.50 above the floor: not enough for a $0.90 order
    note = runner._top_up(s, c2, store, shot, 0.9)
    assert (note and "floor" in note) or "limit" in note
    assert c2.moves == []


def test_the_daily_cap_stops_further_moves(tmp_path, monkeypatch):
    s, store, shot = make(tmp_path, monkeypatch, SHARD_TOPUP_TARGET_USD="10", SHARD_TOPUP_MAX_USD_PER_DAY="10")
    c = Client({0: 0.0, 2: 100.0})
    assert runner._top_up(s, c, store, shot, 0.9) is None  # uses the whole $10 allowance
    c.cash[0] = 0.0  # the exchange is emptied again (a manual trade, say)
    note = runner._top_up(s, c, store, shot, 0.9)
    assert note and "limit" in note and len(c.moves) == 1


def test_place_tops_up_then_orders(tmp_path, monkeypatch):
    s, store, shot = make(tmp_path, monkeypatch, SHARD_TOPUP_TARGET_USD="10")
    c = Client({0: 0.0, 2: 100.0})
    out = runner.place(s, c, store, shot, "why", 3)
    assert "bought" in out.note and c.orders == 1 and c.moves == [(10.0, 2, 0)]


def test_open_positions_are_funded_up_to_the_target_before_trading(tmp_path, monkeypatch):
    s, store, _shot = make(tmp_path, monkeypatch, SHARD_TOPUP_TARGET_USD="10")
    c = Client({0: 1.0, 2: 100.0, 3: 0.0})
    c.positions = lambda: {"market_positions": [
        {"ticker": "KXBTC15M-1", "position_fp": "10", "market_exposure_dollars": "0.50"},
        {"ticker": "KXGOLD15M-1", "position_fp": "-3", "market_exposure_dollars": "2.40"},
    ]}
    c.market = lambda ticker: {"exchange_index": 0 if ticker.startswith("KXBTC") else 3}
    runner.fund_open_exchanges(s, c, store)
    assert c.moves == [(9.0, 2, 0), (10.0, 2, 3)]
    assert c.cash[0] == 10.0 and c.cash[3] == 10.0


def test_an_exchange_with_no_open_position_is_left_alone(tmp_path, monkeypatch):
    s, store, _shot = make(tmp_path, monkeypatch, SHARD_TOPUP_TARGET_USD="10")
    c = Client({0: 0.0, 2: 100.0})
    c.positions = lambda: {"market_positions": []}
    runner.fund_open_exchanges(s, c, store)
    assert c.moves == []


def test_a_transfer_that_never_lands_skips_the_order(tmp_path, monkeypatch):
    s, store, shot = make(tmp_path, monkeypatch, SHARD_TOPUP_TARGET_USD="10")
    c = Client({0: 0.0, 2: 100.0})
    c.shard_transfer = lambda usd, source, src, destination, dst: {"transfer_id": "t"}  # accepted, never credited
    out = runner.place(s, c, store, shot, "why", 3)
    assert "not landed" in out.note and c.orders == 0
