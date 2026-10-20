import pytest

from kalshi_moonshot import target
from kalshi_moonshot.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def bet(store, ticker, outcome, side="yes", ask=0.30, ts=1000.0, contracts=10):
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, outcome, side, contracts, stake_usd)"
        " VALUES (?,?,?,?,?,?,?,?,?)", (ts, ticker, "bet", "observe", ask, outcome, side, contracts, contracts * ask))


def candle(store, ticker, end_ts, bid_hi, ask_lo=None):
    store.db.execute("INSERT INTO candles (ticker, end_ts, bid_hi, ask_lo) VALUES (?,?,?,?)",
                     (ticker, end_ts, bid_hi, ask_lo))


def test_a_touch_after_entry_turns_a_loser_into_a_sale(store):
    bet(store, "KXBTC15M-1", "no")
    candle(store, "KXBTC15M-1", 1200, 0.65)
    rows, missing = target.compare(store, 2.0)
    [r] = rows
    assert r.hit and missing == 0
    assert r.actual == pytest.approx(-3.0)
    assert r.with_target > 0


def test_a_touch_in_the_entry_minute_or_below_target_does_not_count(store):
    bet(store, "KXBTC15M-1", "yes")
    candle(store, "KXBTC15M-1", 1030, 0.90)  # same minute as the bet
    candle(store, "KXBTC15M-1", 1200, 0.50)  # below 2x of 0.30
    [r] = target.compare(store, 2.0)[0]
    assert not r.hit and r.with_target == r.actual


def test_no_side_uses_the_cheapest_yes_ask(store):
    bet(store, "KXBTC15M-1", "yes", side="no", ask=0.30)
    candle(store, "KXBTC15M-1", 1200, 0.10, ask_lo=0.35)  # NO bid reached 0.65
    assert target.compare(store, 2.0)[0][0].hit


def test_bets_without_candles_are_counted_not_scored(store):
    bet(store, "KXBTC15M-1", "yes")
    assert target.compare(store, 2.0) == ([], 1)


def test_a_lane_filter_keeps_only_that_lane(store):
    bet(store, "KXBTC15M-1", "no")
    bet(store, "KXETH15M-1", "no")
    store.db.execute("UPDATE decisions SET lane='late_ticket' WHERE ticker='KXETH15M-1'")
    for t in ("KXBTC15M-1", "KXETH15M-1"):
        candle(store, t, 1200, 0.65)
    assert [r.ticker for r in target.compare(store, 2.0, lane="late_ticket")[0]] == ["KXETH15M-1"]
    assert len(target.compare(store, 2.0)[0]) == 2
