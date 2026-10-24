import pytest

from kalshi_moonshot import config, watcher
from kalshi_moonshot.store import Store


class FakeClient:
    def __init__(self, bid):
        self.bid = bid

    def market(self, ticker):
        return {"ticker": ticker, "yes_bid_dollars": str(self.bid), "yes_ask_dollars": str(self.bid + 0.01)}


def quote(bid, ask, ts=1000.0):
    return {"kind": "quote", "ticker": "KXBTC15M-1", "bid": bid, "ask": ask, "ts": ts}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    store = Store(s.db_path)
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1.0, "KXBTC15M-1", "bet", "observe", 0.30, "yes", "late_ticket", 3, 0.9, "2099-01-01T00:00:00Z"))
    store.db.commit()
    return s, store


def fast_watcher(setup, bid):
    s, _store = setup
    (s.data_dir / "strategies.toml").write_text(
        "[lane.late_ticket]\nestimator='ladder'\nprice=[0.05,0.4]\nhours=[0.0,1.0]\n"
        "trail_activate=0.10\ntrail_drop=0.04\ntrail_confirm_seconds=10\ntrail_hold_seconds=3\ntrail_fast_drop=0.12\n")
    w = watcher.ProfitWatcher(lambda: s, FakeClient(bid), lambda: Store(s.db_path))
    w.refresh()
    return w


def test_a_gap_down_sells_at_once_without_the_confirm_wait(setup):
    w = fast_watcher(setup, 0.28)  # entry 0.30; by the time we look the bid is 0.28, below cost
    for ts, b in ((0.0, 0.40), (5.0, 0.42), (10.0, 0.43)):  # a held peak of 0.42-0.43, armed
        w.on_event(quote(b, b + 0.02, ts=1000.0 + ts))
    w.on_event(quote(0.28, 0.30, ts=1012.0))  # 0.15 below the 0.43 peak in two seconds
    ticker, lane, why = w._jobs.get_nowait()
    assert (ticker, lane) == ("KXBTC15M-1", "late_ticket") and "fast exit" in why


def test_the_sale_is_allowed_below_cost_after_a_peak_crash(setup):
    w = fast_watcher(setup, 0.28)
    for ts, b in ((0.0, 0.40), (5.0, 0.42), (10.0, 0.43)):
        w.on_event(quote(b, b + 0.02, ts=1000.0 + ts))
    w.on_event(quote(0.28, 0.30, ts=1012.0))
    _t, _l, why = w._jobs.get_nowait()
    w._sell("KXBTC15M-1", "late_ticket", why)
    store = Store(setup[0].db_path)
    assert store.open_bets() == []
    xp = store.db.execute("SELECT exit_price FROM decisions WHERE ticker='KXBTC15M-1'").fetchone()["exit_price"]
    assert xp == pytest.approx(0.28)


def test_a_small_dip_does_not_trigger_the_fast_exit(setup):
    w = fast_watcher(setup, 0.40)
    for ts, b in ((0.0, 0.40), (5.0, 0.42), (10.0, 0.43), (11.0, 0.39)):  # 4c off the peak: only the slow rule
        w.on_event(quote(b, b + 0.02, ts=1000.0 + ts))
    assert w._jobs.empty()


def test_a_profit_take_still_refuses_to_sell_below_cost(setup):
    s, _store = setup
    w = watcher.ProfitWatcher(lambda: s, FakeClient(0.28), lambda: Store(s.db_path))
    w.refresh()
    w._sell("KXBTC15M-1", "late_ticket", "take-profit: bid 0.90 is 3.0x the 0.30 paid")
    assert len(Store(s.db_path).open_bets()) == 1


def test_exit_orders_are_limited_under_the_bid_and_a_fast_exit_slips_more(setup):
    import time

    s, store = setup
    s2 = type(s)(**{**s.__dict__, "mode": "trade", "env": "demo", "live_lanes": None, "fast_live": True})
    sold = []

    class Live(FakeClient):
        def sell_yes_ioc(self, ticker, count, price, client_order_id=None):
            sold.append(price)
            return {"fill_count": count, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "s"}

    store.add_order(ticker="KXBTC15M-1", env="demo", count=3, price=0.30, fill_count=3, cost_usd=0.9, fee_usd=0.0, side="buy")
    (s.data_dir / "strategies.toml").write_text(
        "[lane.late_ticket]\nestimator='ladder'\nprice=[0.05,0.4]\nhours=[0.0,1.0]\ntrail_activate=0.10\ntrail_drop=0.04\ntrail_fast_drop=0.12\n")
    w = watcher.ProfitWatcher(lambda: s2, Live(0.60), lambda: Store(s.db_path))
    w.refresh()
    t = w.targets[("KXBTC15M-1", "late_ticket")]
    t.peak, t.last_bid, t.last_ts = 0.80, 0.55, time.time()  # the socket's latest bid (0.55) is lower than REST's (0.60)
    w._sell("KXBTC15M-1", "late_ticket", "peak-crash: bid 0.55 is 0.25 off its 0.80 peak (paid 0.30), fast exit")
    assert sold == [pytest.approx(0.51)]  # min(REST 0.60, socket 0.55) = 0.55, less 2c base slip and 2c more for a fast exit


def test_a_cash_out_is_withheld_when_holding_is_worth_more_than_the_bid(setup):
    s, store = setup
    store.db.execute("UPDATE decisions SET ask=0.80")
    store.db.commit()
    (s.data_dir / "strategies.toml").write_text(
        "[lane.late_ticket]\nestimator='ladder'\nprice=[0.05,0.9]\nhours=[0.0,1.0]\ntrail_activate=0.10\ntrail_drop=0.04\n")
    why = "peak-crash: bid 0.93 is 0.05 off its 0.98 peak (paid 0.80) for 11s"
    for held, sold in ((0.99, False), (0.50, True)):
        store.db.execute("UPDATE decisions SET exit_ts=NULL, exit_price=NULL")
        store.db.commit()
        w = watcher.ProfitWatcher(lambda: s, FakeClient(0.93), lambda: Store(s.db_path), hold_value=lambda row, st, h=held: h)
        w.refresh()
        w.targets[("KXBTC15M-1", "late_ticket")].peak = 0.98  # a held peak, so the dip has not healed
        w._sell("KXBTC15M-1", "late_ticket", why)
        assert (Store(s.db_path).open_bets() == []) is sold, held


def test_an_unknown_hold_value_does_not_block_the_exit(setup):
    s, store = setup
    store.db.execute("UPDATE decisions SET ask=0.80")
    store.db.commit()
    (s.data_dir / "strategies.toml").write_text(
        "[lane.late_ticket]\nestimator='ladder'\nprice=[0.05,0.9]\nhours=[0.0,1.0]\ntrail_activate=0.10\ntrail_drop=0.04\n")
    w = watcher.ProfitWatcher(lambda: s, FakeClient(0.93), lambda: Store(s.db_path), hold_value=lambda row, st: None)
    w.refresh()
    w.targets[("KXBTC15M-1", "late_ticket")].peak = 0.98
    w._sell("KXBTC15M-1", "late_ticket", "peak-crash: bid 0.93 is 0.05 off its 0.98 peak (paid 0.80) for 11s")
    assert Store(s.db_path).open_bets() == []


def test_a_stop_loss_is_hard_by_default_and_only_a_lane_that_asks_lets_the_model_hold_it(setup):
    s, store = setup
    store.db.execute("UPDATE decisions SET ask=0.80")
    store.db.commit()
    why = "stop-loss: bid 0.50 is 38% under the 0.80 paid (socket)"
    for guard, held, sold in (("", 0.85, True), ("", 0.30, True), ("stop_guard=true\n", 0.85, False), ("stop_guard=true\n", 0.30, True), ("stop_guard=true\n", 0.49, True)):
        (s.data_dir / "strategies.toml").write_text(
            "[lane.late_ticket]\nestimator='ladder'\nprice=[0.05,0.9]\nhours=[0.0,1.0]\n" + guard)  # a hard stop sells whatever the model thinks
        store.db.execute("UPDATE decisions SET exit_ts=NULL, exit_price=NULL")
        store.db.commit()
        w = watcher.ProfitWatcher(lambda: s, FakeClient(0.50), lambda: Store(s.db_path), hold_value=lambda row, st, h=held: h)
        w.refresh()
        w._sell("KXBTC15M-1", "late_ticket", why)
        assert (Store(s.db_path).open_bets() == []) is sold, (guard, held)
