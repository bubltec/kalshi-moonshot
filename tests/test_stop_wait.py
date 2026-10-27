"""The stop's wait is calculated from the time left (strategy.stop_wait; fitted in scripts/stop_wait_study.py):
none inside 2 minutes for a cheap contract, where a sale is only a downtrend already under 50c (strategy.price_shape),
45 s at 3-7 minutes, up to 120 s at 2-3, and none past 7 (an early dip is repricing) — hourly markets
keep the lane's cap past 7, and the cap always binds.

A lane that sets watch_at watches that price instead (strategy.watch_trend, strategy.hard_loss). The ride,
the clock it is sized to, the hard loss, and the bounce are that lane's fields."""
import time
from datetime import UTC, datetime

from kalshi_moonshot import config, watcher
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import (
    cash_if_up,
    early_cash,
    hard_loss,
    price_shape,
    ride_for,
    scalp_hit,
    stop_wait,
    watch_trend,
)

TOML = (
    "[lane.fav]\nestimator='ladder'\nprice=[0.5,0.9]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=5.0\n"
    "stop_loss=true\nstop_loss_at=0.45\nstop_confirm_seconds=60\nstop_guard=false\n"
)
WATCH = (
    "watch_at=0.50\nwatch_patient_left=420\nwatch_ride_seconds=45\nwatch_loss=0.75\nwatch_loss_seconds=3\n"
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


def armed(tmp_path, monkeypatch, seconds_left, ticker="KXBTC15M-1", watch=False):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    (s.data_dir / "strategies.toml").write_text(TOML + (WATCH if watch else ""))
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
    w.on_event(quote(0.55, base + 20))  # back above 50c: the wait restarts from zero
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


def test_the_ride_scales_with_the_time_left_and_the_hard_loss_waits_its_bounce():
    assert ride_for(500, 45, 420) == 45  # at least 7 minutes left: the full ride
    assert ride_for(210, 45, 420) == 45 * 210 / 420  # half the clock, half the ride
    assert ride_for(500, 0, 420) == 0
    assert hard_loss(0.80, 0.20, 2, 0.75, 3) is None  # 75% down, still inside the 3s bounce
    assert "watched" in hard_loss(0.80, 0.20, 3, 0.75, 3)
    assert hard_loss(0.80, 0.30, 9, 0.75, 3) is None  # back above the hard line
    assert watch_trend(0.80, 0.48, 500, 10, line=0.50, ride=45, patient_left=420, trend=True) is None
    assert "trending away" in watch_trend(0.80, 0.48, 500, 45, line=0.50, ride=45, patient_left=420, trend=True)
    assert "trending away" in watch_trend(0.80, 0.48, 2000, 45, line=0.50, ride=45, patient_left=420, trend=True)  # an hourly market uses the same ride
    assert watch_trend(0.80, 0.48, 500, 90, line=0.50, ride=45, patient_left=420, trend=False) is None  # a hover is held


def test_a_hover_under_the_watch_is_held_and_a_trend_is_abandoned_after_the_ride(tmp_path, monkeypatch):
    w, base = armed(tmp_path, monkeypatch, 500.0, watch=True)  # more than 7 minutes: the ride is the full 45s
    w.on_event(quote(0.50, base))
    w.on_event(quote(0.48, base + 44))  # still a hover, and inside the ride
    assert w._jobs.empty()
    w.on_event(quote(0.46, base + 45))
    w.on_event(quote(0.42, base + 46))
    w.on_event(quote(0.38, base + 47))  # two steps down, still at the low, and the ride is over
    assert not w._jobs.empty() and "trending away" in w._jobs.get_nowait()[2]


def test_after_minute_7_a_gain_is_sold_and_a_loss_is_kept(tmp_path, monkeypatch):
    assert cash_if_up(0.40, 0.45, 6.2, 7) is None
    assert cash_if_up(0.40, 0.39, 8.0, 7) is None
    assert "above" in cash_if_up(0.40, 0.41, 7.0, 7)
    assert cash_if_up(0.40, 0.29, 8.0, 7, 0.30) is None  # 27% under: still inside the 30% line
    assert cash_if_up(0.40, 0.20, 6.0, 7, 0.30) is None  # before the minute, even a deep drop is kept
    assert "under" in cash_if_up(0.40, 0.28, 8.0, 7, 0.30)
    w, base = armed(tmp_path, monkeypatch, 420.0)  # minute 8 of the window
    t = w.targets[("KXBTC15M-1", "fav")]
    t.entry, t.stop_drop, t.cash_up, t.close_ts = 0.40, 0.0, 7.0, base + 420
    w.on_event(quote(0.39, base))
    assert w._jobs.empty()
    w.on_event(quote(0.41, base + 1))
    assert not w._jobs.empty() and "above" in w._jobs.get_nowait()[2]
    w2, base2 = armed(tmp_path, monkeypatch, 420.0)
    t2 = w2.targets[("KXBTC15M-1", "fav")]
    t2.entry, t2.stop_drop, t2.cash_up, t2.cash_drop, t2.close_ts = 0.40, 0.0, 7.0, 0.30, base2 + 420
    w2.on_event(quote(0.29, base2))  # 27% under
    assert w2._jobs.empty()
    w2.on_event(quote(0.28, base2 + 1))  # 30% under
    assert not w2._jobs.empty() and "under" in w2._jobs.get_nowait()[2]


def test_a_climb_through_70_rides_and_a_3c_dip_sells(tmp_path, monkeypatch):
    assert scalp_hit(0.74, 0.74, 0.70, 0.03) is None  # at the high
    assert scalp_hit(0.72, 0.74, 0.70, 0.03) is None  # 2c under
    assert "scalp" in scalp_hit(0.71, 0.74, 0.70, 0.03)
    assert scalp_hit(0.40, 0.0, 0.70, 0.03) is None  # 70c has not printed
    w, base = armed(tmp_path, monkeypatch, 600.0)
    t = w.targets[("KXBTC15M-1", "fav")]
    t.entry, t.stop_drop, t.scalp_at, t.scalp_dip = 0.40, 0.0, 0.70, 0.03
    w.on_event(quote(0.40, base))
    w.on_event(quote(0.69, base + 1))
    assert w._jobs.empty()
    w.on_event(quote(0.70, base + 2))
    w.on_event(quote(0.74, base + 3))  # the high lifts the line
    w.on_event(quote(0.72, base + 4))
    assert w._jobs.empty()
    w.on_event(quote(0.71, base + 5))  # 3c under 0.74
    assert not w._jobs.empty() and "scalp" in w._jobs.get_nowait()[2]


def test_an_hourly_favorite_uses_the_same_watch(tmp_path, monkeypatch):
    w, base = armed(tmp_path, monkeypatch, 2000.0, ticker="KXGOLDH-1", watch=True)
    w.on_event(quote(0.50, base, "KXGOLDH-1"))
    w.on_event(quote(0.48, base + 44, "KXGOLDH-1"))
    assert w._jobs.empty()
    w.on_event(quote(0.46, base + 45, "KXGOLDH-1"))
    w.on_event(quote(0.42, base + 46, "KXGOLDH-1"))
    w.on_event(quote(0.38, base + 47, "KXGOLDH-1"))
    assert not w._jobs.empty() and "trending away" in w._jobs.get_nowait()[2]


def test_the_hard_loss_sells_after_its_bounce_and_a_recovery_restarts_it(tmp_path, monkeypatch):
    w, base = armed(tmp_path, monkeypatch, 500.0, watch=True)  # 0.80 paid, 75% leaves 0.20
    w.on_event(quote(0.20, base))
    w.on_event(quote(0.20, base + 2))
    assert w._jobs.empty()
    w.on_event(quote(0.30, base + 2.5))  # back above the hard line, still under the watch
    assert w._jobs.empty()
    w.on_event(quote(0.20, base + 4))
    w.on_event(quote(0.20, base + 6))  # only 2s into the new visit
    assert w._jobs.empty()
    w.on_event(quote(0.19, base + 7))
    assert not w._jobs.empty() and "watched" in w._jobs.get_nowait()[2]


def test_a_buy_before_minute_7_cashes_a_profitable_hover():
    assert early_cash(0.84, 0.90, 4.2, 6.0) is None  # ahead, but not hovering and not minute 9 yet
    assert "hovering" in early_cash(0.84, 0.90, 4.2, 6.0, hovering=True)
    assert early_cash(0.84, 0.90, 8.0, 9.0, hovering=True) is None  # bought at minute 8: held
    assert "at minute 9" in early_cash(0.84, 0.88, 4.2, 9.0)  # still ahead when the window reaches minute 9
    assert early_cash(0.84, 0.80, 4.2, 9.0) is None  # under the price paid: the stop decides, not the cash-out


def test_an_early_buy_sells_a_hover_above_the_price_paid(tmp_path, monkeypatch):
    w, base = armed(tmp_path, monkeypatch, 540.0)  # 9 minutes left: minute 6 of the window
    t = w.targets[("KXBTC15M-1", "fav")]
    t.opened_min, t.cash_before, t.cash_leave, t.close_ts = 4.2, 7.0, 9.0, base + 540
    w.on_event(quote(0.90, base))  # one print is not a hover yet
    assert w._jobs.empty()
    w.on_event(quote(0.90, base + 1))  # sitting above the 0.80 paid
    assert not w._jobs.empty() and "hovering" in w._jobs.get_nowait()[2]


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
