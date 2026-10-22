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
    assert set(w.targets) == {("KXBTC15M-1", "late_ticket")} and lanes.takes_profit(lanes.lane_specs(setup[0])["late_ticket"])


def test_a_quote_at_the_level_queues_a_sale_and_a_low_one_does_not(setup):
    w, *_ = make(setup, 0.9)
    w.on_event(quote(0.60, 0.62))
    assert w._jobs.empty()
    w.on_event(quote(0.90, 0.92, ts=1001.0))
    assert w._jobs.get_nowait()[:2] == ("KXBTC15M-1", "late_ticket")


def test_the_sale_rereads_the_market_and_marks_the_paper_exit(setup):
    w, _s, store = make(setup, 0.90)
    w._inflight.add(("KXBTC15M-1", "late_ticket"))
    w._sell("KXBTC15M-1", "late_ticket", "take-profit test")
    assert store.open_bets() == []
    row = store.db.execute("SELECT exit_price FROM decisions WHERE ticker='KXBTC15M-1'").fetchone()
    assert row["exit_price"] == pytest.approx(0.90) and not w._inflight


def test_no_sale_if_the_market_no_longer_shows_a_profit(setup):
    w, _s, store = make(setup, 0.28)  # below the 0.30 paid by the time we look
    w._sell("KXBTC15M-1", "late_ticket", "stale socket quote")
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


def trail_watcher(setup):
    s, _store = setup
    (s.data_dir / "strategies.toml").write_text(
        "[lane.late_ticket]\nestimator='ladder'\nprice=[0.05,0.4]\nhours=[0.0,1.0]\n"
        "trail_activate=0.10\ntrail_drop=0.05\ntrail_confirm_seconds=10\ntrail_hold_seconds=3\n")
    w = watcher.ProfitWatcher(lambda: s, FakeClient(0.33), lambda: Store(s.db_path))
    w.refresh()
    return w


def test_a_held_dip_from_the_peak_sells(setup):
    w = trail_watcher(setup)
    for ts, bid in ((0.0, 0.40), (5.0, 0.41), (10.0, 0.34)):  # entry 0.30; the 0.41 peak held 5s; then down 7c
        w.on_event(quote(bid, bid + 0.02, ts=1000.0 + ts))
    w.check_timers(1015.0)  # only 5s down: ride on
    assert w._jobs.empty()
    w.on_event(quote(0.35, 0.37, ts=1016.0))  # still below the line (0.36)
    w.check_timers(1021.0)  # 11s down
    assert w._jobs.get_nowait()[:2] == ("KXBTC15M-1", "late_ticket")


def test_a_dip_that_heals_quickly_is_ignored(setup):
    w = trail_watcher(setup)
    for ts, bid in ((0.0, 0.40), (5.0, 0.41), (10.0, 0.34), (12.0, 0.40)):  # dips for 2 seconds only
        w.on_event(quote(bid, bid + 0.02, ts=1000.0 + ts))
    w.check_timers(1040.0)
    assert w._jobs.empty()


def test_one_thin_tick_does_not_set_the_peak(setup):
    w = trail_watcher(setup)
    for ts, bid in ((0.0, 0.40), (5.0, 0.60), (6.0, 0.42)):  # 0.60 lasted one second: not a peak
        w.on_event(quote(bid, bid + 0.02, ts=1000.0 + ts))
    assert w.targets[("KXBTC15M-1", "late_ticket")].peak == pytest.approx(0.40)
    w.on_event(quote(0.38, 0.40, ts=1020.0))  # 0.42 held 14s: now it is
    assert w.targets[("KXBTC15M-1", "late_ticket")].peak == pytest.approx(0.42)


def test_no_sale_while_the_trail_has_not_armed(setup):
    w = trail_watcher(setup)
    for ts, bid in ((0.0, 0.31), (5.0, 0.32), (10.0, 0.25)):  # never 10% above the 0.30 paid
        w.on_event(quote(bid, bid + 0.02, ts=1000.0 + ts))
    w.check_timers(1060.0)
    assert w._jobs.empty()

def test_a_spike_must_also_clear_the_minimum_gain():
    hist = [(100.0, 0.28), (110.0, 0.30), (120.0, 0.40)]
    assert spike_hit(hist, 120.0, 0.39, 0.10, 30.0, 0.0) is not None  # up 1c on 39c: the old behavior sold this
    assert spike_hit(hist, 120.0, 0.39, 0.10, 30.0, 0.10) is None  # needs 43c or more
    assert spike_hit([(100.0, 0.20), (120.0, 0.34)], 120.0, 0.30, 0.10, 30.0, 0.10) is not None  # up 13%


def test_a_declined_sale_is_not_rechecked_every_second(setup):
    import time

    w = trail_watcher(setup)  # entry 0.30
    for ts, bid in ((0.0, 0.40), (5.0, 0.41), (10.0, 0.34)):
        w.on_event(quote(bid, bid + 0.02, ts=1000.0 + ts))
    w.check_timers(1021.0)
    assert w._jobs.get_nowait()[:2] == ("KXBTC15M-1", "late_ticket")
    w._inflight.clear()
    w.client = FakeClient(0.40)  # the dip has already healed by the time we look: the sale is declined
    w._sell("KXBTC15M-1", "late_ticket", "peak-crash: bid 0.34 is 0.07 off its 0.41 peak")
    assert w.targets[("KXBTC15M-1", "late_ticket")].retry_after > time.time()
    w.check_timers(time.time() + 5)  # inside the back-off: no new sale is queued
    assert w._jobs.empty()
    w.check_timers(time.time() + 30)  # after it: the dip is still there, so it is looked at again
    assert not w._jobs.empty()


def test_a_spike_that_faded_before_the_sale_is_not_sold(setup):
    w, _s, store = make(setup, 0.31)  # entry 0.30; fresh bid 0.31 is under the 10% minimum gain (0.33)
    w._inflight.add(("KXBTC15M-1", "late_ticket"))
    w._sell("KXBTC15M-1", "late_ticket", "spike: bid 0.40 is up 0.12 in 30s (paid 0.30)")
    assert len(store.open_bets()) == 1
