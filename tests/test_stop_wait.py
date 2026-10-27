"""The stop's wait is calculated from the time left (strategy.stop_wait; fitted in scripts/stop_wait_study.py):
none inside 2 minutes, where a sale is only a downtrend already under 50c (strategy.price_shape),
45 s at 3-7 minutes, up to 120 s at 2-3, and none past 7 (an early dip is repricing) — hourly markets
keep the lane's cap past 7, and the cap always binds."""
import time
from datetime import UTC, datetime

from kalshi_moonshot import config, watcher
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import price_shape, stop_wait

TOML = (
    "[lane.fav]\nestimator='ladder'\nprice=[0.5,0.9]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=5.0\n"
    "stop_loss=true\nstop_loss_at=0.45\nstop_confirm_seconds=60\nstop_guard=false\n"
)


def test_the_wait_follows_the_clock_and_never_exceeds_the_cap():
    assert stop_wait(60.0, None) == 60.0  # close unknown (hourly): the cap, as before
    assert stop_wait(60.0, 3600.0) == 60.0  # past 7 minutes: the cap
    assert stop_wait(60.0, 400.0) == 45.0  # 5-7 minutes left
    assert stop_wait(60.0, 240.0) == 45.0  # 3-5 minutes
    assert stop_wait(60.0, 150.0) == 60.0  # 2-3 minutes: the full minute, dips here are noise
    assert stop_wait(60.0, 90.0) == 0.0  # hourly, inside 2 minutes: the 20s wick-wait is a 15-minute finding
    assert stop_wait(60.0, 30.0) == 0.0
    assert stop_wait(120.0, 90.0, fifteen_min=True) == 0.0  # last 2 minutes: the shape decides, no timer
    assert stop_wait(120.0, 30.0, fifteen_min=True) == 0.0
    assert stop_wait(10.0, 150.0) == 10.0  # a smaller lane cap still binds
    assert stop_wait(0.0, 150.0) == 0.0  # a lane that asked for no wait gets none
    assert stop_wait(120.0, 500.0, fifteen_min=True) == 0.0  # an early dip on a 15-minute market is repricing: out at once
    assert stop_wait(120.0, 500.0) == 120.0  # an hourly market past 7 minutes still waits the cap (the study cannot speak for it)
    assert stop_wait(120.0, 150.0, fifteen_min=True) == 120.0  # the 2-3 minute tier reaches the full 120 s on a 15-minute market
    assert stop_wait(120.0, 150.0) == 60.0  # ...but only a minute on an hourly one
    assert stop_wait(60.0, 150.0, fifteen_min=True) == 60.0  # the lane cap binds there too


class Client:
    def market(self, ticker):
        return {"ticker": ticker, "yes_bid_dollars": "0.30", "yes_ask_dollars": "0.31", "close_time": "2099-01-01T00:00:00Z"}

    def sell_yes_ioc(self, ticker, count, price, client_order_id=None):
        return {"fill_count": count, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "s"}


def quote(bid, ts, ticker="KXBTC15M-1"):
    return {"kind": "quote", "ticker": ticker, "bid": bid, "ask": bid + 0.01, "ts": ts}


def armed(tmp_path, monkeypatch, seconds_left, ticker="KXBTC15M-1"):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    (s.data_dir / "strategies.toml").write_text(TOML)
    store = Store(s.db_path)
    base = time.time()
    close = datetime.fromtimestamp(base + seconds_left, UTC).isoformat()
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1.0, ticker, "bet", "observe", 0.80, "yes", "fav", 6, 4.9, close))
    store.db.commit()
    w = watcher.ProfitWatcher(lambda: s, Client(), lambda: Store(s.db_path))
    w.refresh()
    t = w.targets[(ticker, "fav")]
    t.stop_from = 0.0
    return w, base


def test_mid_market_the_stop_waits_45_seconds_for_a_rebound(tmp_path, monkeypatch):
    w, base = armed(tmp_path, monkeypatch, 240.0)  # 4 minutes left when the dip starts
    w.on_event(quote(0.40, base))  # half under the 0.80 paid: past the 45% line
    w.on_event(quote(0.40, base + 30))
    assert w._jobs.empty()  # still inside the calculated wait
    w.on_event(quote(0.40, base + 46))
    assert not w._jobs.empty() and "stop-loss" in w._jobs.get_nowait()[2]


def test_a_rebound_inside_the_wait_disarms_the_stop(tmp_path, monkeypatch):
    w, base = armed(tmp_path, monkeypatch, 240.0)
    w.on_event(quote(0.40, base))
    w.on_event(quote(0.50, base + 20))  # back above the line: the wait restarts from zero
    w.on_event(quote(0.40, base + 30))
    w.on_event(quote(0.40, base + 60))  # only 30 s into the new dip
    assert w._jobs.empty()
    w.on_event(quote(0.40, base + 91))  # 61 s in, and under 3 minutes left now, where the wait is the full minute
    assert not w._jobs.empty()


def test_the_three_shapes():
    assert price_shape([(0, 0.57), (1, 0.57), (2, 0.51), (3, 0.25)], 3, 0.25) == "trend"  # gold, 8:44:50
    assert price_shape([(0, 0.42), (1, 0.42), (2, 0.25)], 2, 0.25) == "spike"  # one gap, not a second step down
    assert price_shape([(0, 0.42), (1, 0.25), (2, 0.39)], 2, 0.39) == "spike"  # retraced more than half
    assert price_shape([(0, 0.40), (1, 0.42), (2, 0.39)], 2, 0.39) == "hover"


def test_a_downtrend_under_50_sells_on_that_print(tmp_path, monkeypatch):
    w, base = armed(tmp_path, monkeypatch, 20.0)
    w.on_event(quote(0.57, base))  # still above the 0.44 line
    w.on_event(quote(0.51, base + 1))
    assert w._jobs.empty()
    w.on_event(quote(0.25, base + 2))  # under 50c, two steps down, still at the low
    assert not w._jobs.empty() and "stop-loss" in w._jobs.get_nowait()[2]


def test_a_single_gap_and_a_hover_are_not_sold(tmp_path, monkeypatch):
    w, base = armed(tmp_path, monkeypatch, 20.0)
    w.on_event(quote(0.42, base))
    w.on_event(quote(0.25, base + 1))  # one gap under 50c: a spike, not a trend
    w.on_event(quote(0.40, base + 3))
    w.on_event(quote(0.41, base + 4))  # hovering under the line
    w.on_event(quote(0.39, base + 5))
    assert w._jobs.empty()


def test_past_seven_minutes_a_15m_dip_sells_at_once(tmp_path, monkeypatch):
    w, base = armed(tmp_path, monkeypatch, 500.0)
    w.on_event(quote(0.40, base))  # an early dip is the favorite being repriced; waiting only bleeds
    assert not w._jobs.empty() and "stop-loss" in w._jobs.get_nowait()[2]


def test_past_seven_minutes_an_hourly_market_still_waits_the_cap(tmp_path, monkeypatch):
    w, base = armed(tmp_path, monkeypatch, 500.0, ticker="KXBTC-1")
    w.on_event(quote(0.40, base, "KXBTC-1"))
    w.on_event(quote(0.40, base + 46, "KXBTC-1"))
    assert w._jobs.empty()  # the early-out is a 15-minute finding; the hourly stop waits its 60 s cap
    w.on_event(quote(0.40, base + 61, "KXBTC-1"))
    assert not w._jobs.empty()
