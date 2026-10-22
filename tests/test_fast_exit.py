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
