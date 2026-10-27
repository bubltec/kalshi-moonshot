from datetime import UTC, datetime, timedelta

import pytest

from kalshi_moonshot import config, runner
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot


def settings(tmp_path, **over):
    s = config.load()
    base = {"data_dir": tmp_path, "mode": "trade", "env": "demo", "min_order_usd": 0.0,
            "paper_bankroll_usd": 100.0, "skip_categories": frozenset(), "commodities_enabled": True,
            "max_usd_per_trade": 5.0, "max_loss_per_day": 100.0, "max_open_positions": 20}
    return type(s)(**{**s.__dict__, **base, **over})


def test_live_lanes_setting(monkeypatch):
    monkeypatch.delenv("LIVE_LANES", raising=False)
    assert config.load().live_lanes is None
    monkeypatch.setenv("LIVE_LANES", "Commodity, steady")
    assert config.load().live_lanes == frozenset({"commodity", "steady"})
    monkeypatch.setenv("LIVE_LANES", "none")
    assert config.load().live_lanes == frozenset()


def test_is_live(tmp_path):
    everything = settings(tmp_path, live_lanes=None)
    assert everything.is_live("moonshot") and everything.is_live("commodity")  # the old meaning
    only = settings(tmp_path, live_lanes=frozenset({"commodity"}))
    assert only.is_live("commodity") and not only.is_live("moonshot")
    assert not settings(tmp_path, mode="observe", live_lanes=frozenset({"commodity"})).is_live("commodity")
    assert not settings(tmp_path, live_lanes=frozenset()).is_live("commodity")
    assert not settings(tmp_path, live_lanes=None).is_live("commodity_poly")
    assert not settings(tmp_path, live_lanes=frozenset({"commodity_poly"})).is_live("commodity_poly", "KXGOLD15M")


class Client:
    authed = True

    def __init__(self):
        self.bought: list[str] = []

    def balance(self):
        return {"balance": 20000, "balance_dollars": "200.0000", "portfolio_value": 0}

    def orderbook(self, ticker):
        return {"orderbook_fp": {"no_dollars": [["0.9000", "500.00"]], "yes_dollars": [["0.0500", "500.00"]]}}

    def buy_yes_ioc(self, ticker, count, price, client_order_id=None):
        self.bought.append(ticker)
        return {"fill_count": count, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "o"}

    def buy_no_ioc(self, ticker, count, price, client_order_id=None):
        self.bought.append(ticker)
        return {"fill_count": count, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "o"}

    def fill_event_info(self, tickers):
        return {}


class Est:
    def estimate(self, *a, **k):
        return 0.30, "because"


def shot(ticker, lane, ask=0.10):
    close = (datetime.now(UTC) + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return LongShot(ticker, "t", "", ask - 0.01, ask, 500.0, close, 10.0, lane=lane, event_ticker=ticker)


def test_only_the_named_lane_places_orders_the_rest_stay_paper(tmp_path):
    s = settings(tmp_path, live_lanes=frozenset({"steady"}))
    store = Store(s.db_path)
    client = Client()
    paper = runner.evaluate(s, client, Est(), store, shot("KXM-1", "moonshot"))
    assert paper.action == "bet" and client.bought == []  # sized and recorded, never sent
    row = store.db.execute("SELECT mode, contracts, stake_usd FROM decisions WHERE ticker='KXM-1'").fetchone()
    assert (row["mode"], row["contracts"] > 0, row["stake_usd"] > 0) == ("observe", True, True)
    assert store.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0

    live = settings(tmp_path, live_lanes=frozenset({"moonshot"}))
    runner.evaluate(live, client, Est(), store, shot("KXM-2", "moonshot"))
    assert client.bought == ["KXM-2"]
    assert store.db.execute("SELECT mode FROM decisions WHERE ticker='KXM-2'").fetchone()[0] == "trade"


def test_a_paper_lane_is_sized_from_the_paper_balance_not_the_real_one(tmp_path):
    s = settings(tmp_path, live_lanes=frozenset({"steady"}))
    store = Store(s.db_path)
    client = Client()
    assert store.bankroll.read(client, s, live=True).source == "kalshi"
    assert store.bankroll.read(client, s, live=False).source == "paper"


def test_stop_loss_only_sells_for_real_on_live_lanes(tmp_path):
    close = (datetime.now(UTC) + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    s = settings(tmp_path, live_lanes=frozenset({"steady"}), stop_loss_enabled=True,
                 stop_loss_hours=1.0, stop_loss_drop=0.3)
    store = Store(s.db_path)
    for ticker, lane in (("KXP-1", "moonshot"), ("KXL-1", "steady")):
        store.add_decision(ticker=ticker, title="t", bid=0.4, ask=0.5, model_p=0.9, edge=0.1,
                           action="bet", mode="trade", close_time=close, lane=lane)
        store.add_order(ticker=ticker, env="demo", count=2, price=0.5, fill_count=2,
                        cost_usd=1.0, fee_usd=0.0)

    class C:
        def __init__(self):
            self.sold: list[str] = []

        def market(self, ticker):
            return {"yes_bid_dollars": "0.2000", "yes_ask_dollars": "0.2500"}

        def sell_yes_ioc(self, ticker, count, price, client_order_id=None):
            self.sold.append(ticker)
            return {"fill_count": count, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "s"}

    c = C()
    notes = runner.stop_loss_pass(s, c, store)
    assert c.sold == ["KXL-1"]  # the live lane sells for real
    assert any("paper-sold KXP-1" in n for n in notes)  # the paper lane only records it


@pytest.mark.parametrize("mode", ["observe", "trade"])
def test_commodity_is_live_only_when_named(tmp_path, mode):
    s = settings(tmp_path, mode=mode, live_lanes=frozenset({"commodity"}))
    assert s.is_live("commodity") == (mode == "trade")


def test_a_live_lane_counts_only_real_bets_against_its_exposure(tmp_path):
    s = settings(tmp_path, live_lanes=frozenset({"commodity"}))
    store = Store(s.db_path)
    # an old paper bet from before the lane went live, and a real one just now
    store.add_decision(ticker="KXC-PAPER", title="t", bid=0.9, ask=0.95, model_p=0.99, edge=0.04,
                       action="bet", mode="observe", lane="commodity", contracts=10, stake_usd=9.5)
    store.add_decision(ticker="KXC-REAL", title="t", bid=0.9, ask=0.95, model_p=0.99, edge=0.04,
                       action="bet", mode="trade", lane="commodity", contracts=4, stake_usd=3.8)
    live = store.bankroll.read(Client(), s, live=True)
    assert live.source == "kalshi" and live.open_by_lane == {"commodity": pytest.approx(3.8)}
    paper = store.bankroll.read(Client(), s, live=False)
    assert paper.open_by_lane == {"commodity": pytest.approx(9.5)}


# ---- what is recorded as a bet must be a position we actually hold ----
class FillClient(Client):
    def __init__(self, filled):
        super().__init__()
        self.filled = filled

    def buy_yes_ioc(self, ticker, count, price, client_order_id=None):
        self.bought.append(ticker)
        return {"fill_count": self.filled, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "o"}


def test_a_bet_blocked_by_the_risk_gate_is_not_recorded_as_a_bet(tmp_path):
    s = settings(tmp_path, live_lanes=frozenset({"moonshot"}), max_open_positions=0)
    store = Store(s.db_path)
    out = runner.evaluate(s, Client(), Est(), store, shot("KXB-1", "moonshot"))
    assert out.action == "pass" and "blocked by risk" in out.note
    row = store.db.execute("SELECT action, contracts, stake_usd FROM decisions").fetchone()
    assert (row["action"], row["contracts"], row["stake_usd"]) == ("pass", None, None)
    assert store.paper_bets(open_only=True, live=True) == []  # no phantom exposure


def test_a_partial_fill_is_recorded_at_the_size_that_filled(tmp_path):
    s = settings(tmp_path, live_lanes=frozenset({"moonshot"}))
    store = Store(s.db_path)
    out = runner.evaluate(s, FillClient(1), Est(), store, shot("KXP-1", "moonshot", ask=0.20))
    assert out.action == "bet"
    row = store.db.execute("SELECT contracts, stake_usd FROM decisions").fetchone()
    assert row["contracts"] == 1 and row["stake_usd"] == pytest.approx(0.20, abs=0.02)


def test_a_fractional_fill_is_not_chopped_down_to_a_whole_contract(tmp_path):
    s = settings(tmp_path, live_lanes=frozenset({"moonshot"}))
    store = Store(s.db_path)
    out = runner.evaluate(s, FillClient(1.28), Est(), store, shot("KXP-2", "moonshot", ask=0.20))
    assert out.action == "bet"
    row = store.db.execute("SELECT contracts FROM decisions").fetchone()
    assert row["contracts"] == pytest.approx(1.28)


def test_an_order_that_does_not_fill_leaves_no_position(tmp_path):
    s = settings(tmp_path, live_lanes=frozenset({"moonshot"}))
    store = Store(s.db_path)
    out = runner.evaluate(s, FillClient(0), Est(), store, shot("KXZ-1", "moonshot"))
    assert out.action == "pass"
    assert store.paper_bets(open_only=True, live=True) == []


def test_15_minute_windows_paper_trade_until_switched_on(tmp_path):
    s = settings(tmp_path, live_lanes=frozenset({"commodity"}), fast_live=False)
    assert s.is_live("commodity", "KXGOLDD") and s.is_live("commodity")  # slower markets are live
    assert not s.is_live("commodity", "KXGOLD15M") and not s.is_live("commodity", "KXBTC15M")
    on = settings(tmp_path, live_lanes=frozenset({"commodity"}), fast_live=True)
    assert on.is_live("commodity", "KXGOLD15M")
    assert not settings(tmp_path, mode="observe", fast_live=True).is_live("commodity", "KXGOLD15M")


def test_a_15_minute_bet_is_sized_and_logged_but_never_sent_while_paper(tmp_path):
    s = settings(tmp_path, live_lanes=frozenset({"commodity"}), fast_live=False)
    store = Store(s.db_path)
    client = Client()
    fast = shot("KXGOLD15M-X-15", "moonshot")  # any lane; the series decides
    fast = LongShot(**{**fast.__dict__, "series": "KXGOLD15M"})
    out = runner.evaluate(live_all(s), client, Est(), store, fast)
    assert out.action == "bet" and client.bought == []
    row = store.db.execute("SELECT mode, stake_usd FROM decisions").fetchone()
    assert row["mode"] == "observe" and row["stake_usd"] > 0


def live_all(s):
    from dataclasses import replace

    return replace(s, live_lanes=None)


def test_a_misspelled_live_lane_is_reported_not_silently_ignored(tmp_path):
    from kalshi_moonshot import lanes

    s = settings(tmp_path, live_lanes=frozenset({"commodity", "15min"}))
    assert lanes.unknown_live_lanes(s) == ["15min"]
    assert lanes.unknown_live_lanes(settings(tmp_path, live_lanes=frozenset({"commodity"}))) == []
    assert lanes.unknown_live_lanes(settings(tmp_path, live_lanes=None)) == []
    assert lanes.unknown_live_lanes(settings(tmp_path, live_lanes=frozenset())) == []


class PriceClient(Client):
    def __init__(self):
        super().__init__()
        self.prices: list[float] = []

    def buy_yes_ioc(self, ticker, count, price, client_order_id=None):
        self.prices.append(price)
        return super().buy_yes_ioc(ticker, count, price, client_order_id)


def test_a_lane_can_allow_a_few_cents_over_the_ask_on_a_buy(tmp_path):
    (tmp_path / "plain").mkdir()
    (tmp_path / "slip").mkdir()
    plain = settings(tmp_path / "plain", live_lanes=frozenset({"moonshot"}))
    (tmp_path / "slip" / "strategies.toml").write_text("[lane.moonshot]\nbuy_slip = 0.02\n")
    slip = settings(tmp_path / "slip", live_lanes=frozenset({"moonshot"}))
    for s, expected in ((plain, 0.10), (slip, 0.12)):
        store = Store(s.db_path)
        client = PriceClient()
        runner.evaluate(s, client, Est(), store, shot("KXM-9", "moonshot", ask=0.10))
        assert client.prices == [pytest.approx(expected)]  # the limit is the ask plus the lane's allowance; the decision still records the ask it read
        assert store.db.execute("SELECT price FROM orders").fetchone()[0] == pytest.approx(expected)
        assert store.db.execute("SELECT ask FROM decisions WHERE ticker='KXM-9'").fetchone()[0] == pytest.approx(0.10)


def test_the_allowance_never_takes_a_limit_past_99_cents(tmp_path):
    (tmp_path / "strategies.toml").write_text("[lane.moonshot]\nbuy_slip = 0.05\n")
    s = settings(tmp_path, live_lanes=frozenset({"moonshot"}))
    client = PriceClient()
    runner.evaluate(s, client, Est(), Store(s.db_path), shot("KXM-8", "moonshot", ask=0.97))
    assert all(p <= 0.99 + 1e-9 for p in client.prices)


def test_a_sub_cent_ask_is_never_bid_under(tmp_path):
    (tmp_path / "strategies.toml").write_text("[lane.moonshot]\nprice = [0.50, 0.99]\n")
    s = settings(tmp_path, live_lanes=frozenset({"moonshot"}))

    class Deep(PriceClient):
        def orderbook(self, ticker):  # NO bids at 0.086 are YES offered at 0.914
            return {"orderbook_fp": {"no_dollars": [["0.0860", "500.00"]], "yes_dollars": [["0.0500", "500.00"]]}}

    client = Deep()
    class Sure:
        def estimate(self, *a, **k):
            return 0.99, "sure"

    out = runner.evaluate(s, client, Sure(), Store(s.db_path), shot("KXM-7", "moonshot", ask=0.914))
    assert client.prices, out
    assert client.prices == [pytest.approx(0.914)]  # rounding the limit to cents would have made it 0.91, under the ask, and the order would not fill
