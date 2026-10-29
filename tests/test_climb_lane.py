from datetime import UTC, datetime

import pytest

from kalshi_moonshot import config, watcher
from kalshi_moonshot.store import Store

CLOSE = datetime(2030, 1, 1, 0, 15, tzinfo=UTC).timestamp()
TICKER = "KXBTC15M-30JAN010015-15"

LANE = """[lane.climb_down]
estimator='ladder'
sides=['no']
price=[0.40,0.43]
hours=[0.0,0.26]
timed_suffix='15M'
stop_loss=false
take_profit_at=0.50
rest_sell=true
trail_progress=0
trail_activate=0.07
trail_giveback=1.0
trail_drop=0.03
trail_confirm_seconds=0
trail_hold_seconds=1
cash_up_minute=13
cash_drop=0.01
cash_left_seconds=5
"""


@pytest.fixture
def held(tmp_path, monkeypatch):
    """A NO bet bought at 0.41 on the DOWN climb lane, with its watcher."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE)
    s = config.load()
    store = Store(s.db_path)
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (CLOSE - 600, TICKER, "bet", "observe", 0.41, "no", "climb_down", 5, 2.05, "2030-01-01T00:15:00Z"))
    store.db.commit()
    w = watcher.ProfitWatcher(lambda: s, object(), lambda: Store(s.db_path))
    w.refresh()
    assert ("KXBTC15M-30JAN010015-15", "climb_down") in w.targets
    return w


def no_bid(w, bid, left):
    """A quote that leaves the NO side's bid at `bid`, `left` seconds before the close (the YES book is the mirror image)."""
    w.on_event({"kind": "quote", "ticker": TICKER, "bid": round(1.0 - bid - 0.02, 4), "ask": round(1.0 - bid, 4), "ts": CLOSE - left})


def signals(w):
    out = []
    while not w._jobs.empty():
        out.append(w._jobs.get_nowait())
    return out


def test_the_resting_sell_target_is_fifty_cents(held):
    no_bid(held, 0.44, 400)
    assert signals(held) == []
    no_bid(held, 0.50, 399)
    assert [x.why.split(":")[0] for x in signals(held)] == ["take-profit"]


def test_the_lock_sells_when_a_gain_of_three_cents_is_given_back_to_the_price_paid(held):
    for k, bid in enumerate((0.41, 0.45, 0.45, 0.45)):
        no_bid(held, bid, 400 - k)
    assert signals(held) == []  # up 4c and holding: nothing to sell
    no_bid(held, 0.43, 396)
    held.check_timers(CLOSE - 395)
    assert signals(held) == []  # still above the price paid
    no_bid(held, 0.41, 394)
    held.check_timers(CLOSE - 393)
    assert [x.why.split(":")[0] for x in signals(held)] == ["peak-crash"]


def test_a_dip_that_never_got_up_is_left_alone_until_120_seconds_remain(held):
    for k, bid in enumerate((0.41, 0.38, 0.35, 0.33)):
        no_bid(held, bid, 400 - k)
        held.check_timers(CLOSE - 399 + k)
    assert signals(held) == []  # no stop-loss, no lock before a gain: the 50c sell and the timed exit are the plan
    no_bid(held, 0.30, 100)  # 100 s left is past minute 13: a bid under the price paid goes too
    assert [x.why.split(":")[0] for x in signals(held)] == ["cash-out"]


def test_at_120_seconds_a_bid_above_the_price_paid_is_sold(held):
    no_bid(held, 0.43, 300)
    assert signals(held) == []
    no_bid(held, 0.43, 110)
    assert [x.why.split(":")[0] for x in signals(held)] == ["cash-out"]


COMMODITY = """[lane.commodity_t]
estimator='ladder'
sides=['yes']
price=[0.78,0.92]
hours=[0.0,0.26]
timed_suffix='15M'
stop_loss=false
settlement_flip=true
burst_dollars=400
cash_left_seconds=5
"""


@pytest.fixture
def commodity(tmp_path, monkeypatch):
    """A YES bet bought at 0.84 on a commodity-style lane that has no stop, only the burst exit and the last-seconds cash-out."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(COMMODITY)
    s = config.load()
    store = Store(s.db_path)
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (CLOSE - 600, TICKER, "bet", "observe", 0.84, "yes", "commodity_t", 5, 4.2, "2030-01-01T00:15:00Z"))
    store.db.commit()
    w = watcher.ProfitWatcher(lambda: s, object(), lambda: Store(s.db_path))
    w.refresh()
    return w


def yes_bid(w, bid, left):
    w.on_event({"kind": "quote", "ticker": TICKER, "bid": bid, "ask": round(bid + 0.02, 4), "ts": CLOSE - left})


def test_a_lane_with_no_stop_is_still_watched_and_a_deep_drop_mid_window_is_left_alone(commodity):
    assert (TICKER, "commodity_t") in commodity.targets  # still followed, so the burst exit and the cash-out can run
    yes_bid(commodity, 0.30, 300)
    commodity.check_timers(CLOSE - 299)
    assert signals(commodity) == []  # down 64% with five minutes left and nothing sells it


def test_in_the_last_seconds_a_hover_under_the_price_paid_is_sold(commodity):
    for left in (300, 299, 298):
        yes_bid(commodity, 0.30, left)
    assert signals(commodity) == []
    for left in (7, 6, 5, 4):
        yes_bid(commodity, 0.30, left)
    assert [x.why.split(":")[0] for x in signals(commodity)] == ["cash-out"]  # 0.30 against 0.84 paid, hovering, 5 s left: out before it settles at nothing


def test_in_the_last_seconds_a_bid_at_or_above_the_price_paid_is_left_to_settle(commodity):
    for left in (7, 6, 5, 4):
        yes_bid(commodity, 0.90, left)
    assert signals(commodity) == []
