import pytest

from kalshi_moonshot import config, watcher
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import take_profit_hit


def test_take_profit_rules():
    assert take_profit_hit(0.30, 0.90, x=3.0) is not None
    assert take_profit_hit(0.30, 0.80, x=3.0) is None
    assert take_profit_hit(0.60, 0.99, x=3.0) is None  # 3x of 60c is above $1: this rule cannot apply
    assert take_profit_hit(0.80, 0.95, at=0.95) is not None
    assert take_profit_hit(0.96, 0.97, at=0.95) is None  # already paid more than the level


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
    toml = "[lane.late_ticket]\nestimator='ladder'\nprice=[0.05,0.4]\nhours=[0.0,1.0]\ntake_profit_x=3.0\nspike_cents=0.15\n"
    (tmp_path / "strategies.toml").write_text(toml)
    return s, store


class FakeClient:
    def __init__(self, bid):
        self.bid = bid

    def market(self, ticker):
        return {"ticker": ticker, "yes_bid_dollars": str(self.bid), "yes_ask_dollars": str(self.bid + 0.01)}


def make(setup, bid):
    s, store = setup
    w = watcher.ProfitWatcher(lambda: s, FakeClient(bid), lambda: Store(s.db_path))
    w.refresh()
    return w, s, store


def quote(bid, ask, ts=1000.0):
    return {"kind": "quote", "ticker": "KXBTC15M-1", "bid": bid, "ask": ask, "ts": ts}


def test_peak_crash_needs_the_gain_first_then_the_fall():
    from kalshi_moonshot.strategy import trail_hit

    assert trail_hit(0.30, 0.36, 0.31, 0.10, 0.04) is not None  # up 20%, then 5c off the peak
    assert trail_hit(0.30, 0.31, 0.27, 0.10, 0.04) is None  # never reached +10%: that is a loss, not a peak crash
    assert trail_hit(0.30, 0.36, 0.34, 0.10, 0.04) is None  # only 2c off the peak
    assert trail_hit(0.30, 0.36, 0.31, 0.10, 0.0) is None  # off


def trail_watcher(setup):
    s, _store = setup
    (s.data_dir / "strategies.toml").write_text(
        "[lane.late_ticket]\nestimator='ladder'\nprice=[0.05,0.4]\nhours=[0.0,1.0]\n"
        "trail_drop=0.05\ntrail_confirm_seconds=10\ntrail_hold_seconds=3\n")
    w = watcher.ProfitWatcher(lambda: s, FakeClient(0.33), lambda: Store(s.db_path))
    w.refresh()
    return w


def stop_watcher(setup, bid=0.80):
    s, store = setup
    (s.data_dir / "strategies.toml").write_text("[lane.late_ticket]\nestimator='ladder'\nprice=[0.05,0.9]\nhours=[0.0,1.0]\n")
    import time as _time

    soon = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_time.time() + 600))  # a 15-minute window ending in ten minutes
    store.db.execute("UPDATE decisions SET ask=0.80, close_time=?", (soon,))
    store.db.commit()
    w = watcher.ProfitWatcher(lambda: s, FakeClient(bid), lambda: Store(s.db_path))
    w.refresh()
    return w


def test_the_stop_sale_goes_through_below_cost(setup):
    w = stop_watcher(setup, bid=0.40)
    w._inflight.add(("KXBTC15M-1", "late_ticket"))
    w._sell("KXBTC15M-1", "late_ticket", "stop-loss: bid 0.40 is 50% under the 0.80 paid (socket)")
    assert Store(setup[0].db_path).open_bets() == []


def test_an_hourly_market_is_only_stopped_in_its_last_hour(setup):
    s, store = setup
    (s.data_dir / "strategies.toml").write_text("[lane.late_ticket]\nestimator='ladder'\nprice=[0.05,0.9]\nhours=[0.0,1.0]\n")
    store.db.execute("UPDATE decisions SET ask=0.80, close_time='2099-01-01T00:00:00Z'")  # decades away: outside the last hour
    store.db.commit()
    w = watcher.ProfitWatcher(lambda: s, FakeClient(0.5), lambda: Store(s.db_path))
    w.refresh()
    w.on_event(quote(0.40, 0.42, ts=1000.0))
    assert w._jobs.empty()


def test_a_fixed_take_profit_covers_the_entries_a_multiple_cannot_reach():
    assert take_profit_hit(0.80, 0.99, x=1.5, at=0.99) is not None  # 1.5x of 80c is above $1, so the fixed price is what sells
    assert take_profit_hit(0.80, 0.98, x=1.5, at=0.99) is None
    assert take_profit_hit(0.50, 0.75, x=1.5, at=0.99) is not None  # a cheap entry still sells at 1.5x, long before the fixed price
    assert take_profit_hit(0.99, 0.99, x=1.5, at=0.99) is None  # already paid the level


def test_the_store_reads_the_fill_price_without_the_fee_and_none_without_orders(setup):
    _s, store = setup
    assert store.entry_fill("KXBTC15M-1", "yes", 100.0) is None
    store.add_order(ts=101.0, ticker="KXBTC15M-1", env="prod", count=4.0, price=0.40, fill_count=4.0, cost_usd=1.68, fee_usd=0.08, order_id="a", side="buy", contract="yes")
    store.add_order(ts=102.0, ticker="KXBTC15M-1", env="prod", count=4.0, price=0.50, fill_count=4.0, cost_usd=2.07, fee_usd=0.07, order_id="b", side="buy", contract="yes")  # a top-up
    assert abs(store.entry_fill("KXBTC15M-1", "yes", 100.0) - 0.45) < 1e-9  # (1.60 + 2.00) / 8
    assert store.entry_fill("KXBTC15M-1", "yes", 200.0) is None  # orders before the bet are not this position's


def _extreme(entry, gain_x=2.0, high=0.95, high_left=420.0, min_gain=0.10):
    import time

    now = time.time()
    t = watcher.Target(ticker="KXSOL15M-X", lane="crypto_multiscale", side="yes", entry=entry, x=0.0, at=0.0)
    t.value_gain_x, t.value_high, t.value_high_left, t.value_min_gain = gain_x, high, high_left, min_gain
    return t, now

