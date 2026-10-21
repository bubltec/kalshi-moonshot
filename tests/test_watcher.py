import pytest

from kalshi_moonshot import config, lanes, runner, watcher
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import spike_hit, take_profit_hit


def test_take_profit_rules():
    assert take_profit_hit(0.30, 0.90, x=3.0) is not None
    assert take_profit_hit(0.30, 0.80, x=3.0) is None
    assert take_profit_hit(0.60, 0.99, x=3.0) is None  # 3x of 60c is above $1: this rule cannot apply
    assert take_profit_hit(0.80, 0.95, at=0.95) is not None
    assert take_profit_hit(0.96, 0.97, at=0.95) is None  # already paid more than the level


def test_spike_needs_a_rise_inside_the_window_and_a_profit():
    hist = [(100.0, 0.20), (110.0, 0.22), (120.0, 0.34)]
    assert spike_hit(hist, 120.0, 0.25, 0.10, 30.0) is not None
    assert spike_hit(hist, 120.0, 0.40, 0.10, 30.0) is None  # still under the price paid
    assert spike_hit(hist, 120.0, 0.25, 0.10, 5.0) is None  # the low was outside the window
    assert spike_hit(hist[:1], 100.0, 0.25, 0.10, 30.0) is None


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


def test_the_watcher_follows_only_bets_whose_lane_takes_profit(setup):
    w, *_ = make(setup, 0.9)
    assert set(w.targets) == {"KXBTC15M-1"} and lanes.takes_profit(lanes.lane_specs(setup[0])["late_ticket"])


def test_a_quote_at_the_level_queues_a_sale_and_a_low_one_does_not(setup):
    w, *_ = make(setup, 0.9)
    w.on_event(quote(0.60, 0.62))
    assert w._jobs.empty()
    w.on_event(quote(0.90, 0.92, ts=1001.0))
    assert w._jobs.get_nowait()[0] == "KXBTC15M-1"


def test_the_sale_rereads_the_market_and_marks_the_paper_exit(setup):
    w, _s, store = make(setup, 0.90)
    w._inflight.add("KXBTC15M-1")
    w._sell("KXBTC15M-1", "take-profit test")
    assert store.open_bets() == []
    row = store.db.execute("SELECT exit_price FROM decisions WHERE ticker='KXBTC15M-1'").fetchone()
    assert row["exit_price"] == pytest.approx(0.90) and not w._inflight


def test_no_sale_if_the_market_no_longer_shows_a_profit(setup):
    w, _s, store = make(setup, 0.28)  # below the 0.30 paid by the time we look
    w._sell("KXBTC15M-1", "stale socket quote")
    assert len(store.open_bets()) == 1


def test_the_default_lanes_do_not_start_a_watcher(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert not watcher.wanted(config.load())
    assert runner.next_stop_loss_at(config.load(), Store(config.load().db_path)) is None


def test_peak_crash_needs_the_gain_first_then_the_fall():
    from kalshi_moonshot.strategy import trail_hit

    assert trail_hit(0.30, 0.36, 0.31, 0.10, 0.04) is not None  # up 20%, then 5c off the peak
    assert trail_hit(0.30, 0.31, 0.27, 0.10, 0.04) is None  # never reached +10%: that is a loss, not a peak crash
    assert trail_hit(0.30, 0.36, 0.34, 0.10, 0.04) is None  # only 2c off the peak
    assert trail_hit(0.30, 0.36, 0.31, 0.10, 0.0) is None  # off


def test_the_watcher_tracks_the_peak_and_sells_on_the_crash(setup):
    s, _store = setup
    (s.data_dir / "strategies.toml").write_text(
        "[lane.late_ticket]\nestimator='ladder'\nprice=[0.05,0.4]\nhours=[0.0,1.0]\ntrail_activate=0.10\ntrail_drop=0.05\n")
    w = watcher.ProfitWatcher(lambda: s, FakeClient(0.33), lambda: Store(s.db_path))
    w.refresh()
    w.on_event(quote(0.40, 0.42, ts=1000.0))  # up 33%: the peak, no sale
    assert w._jobs.empty() and w.targets["KXBTC15M-1"].peak == pytest.approx(0.40)
    w.on_event(quote(0.34, 0.36, ts=1001.0))  # 6c off the peak
    assert w._jobs.get_nowait()[0] == "KXBTC15M-1"
