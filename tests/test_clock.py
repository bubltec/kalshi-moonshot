"""15-minute favorites sliced by the New York clock: this hour vs the same hour last week, then weekday."""
from datetime import datetime
from zoneinfo import ZoneInfo

from kalshi_moonshot import clock
from kalshi_moonshot.store import Store

ET = ZoneInfo("America/New_York")


def ts(y, m, d, h, minute=0):
    return datetime(y, m, d, h, minute, tzinfo=ET).timestamp()


def bet(store, ticker, lane, ask, side, outcome, when, exit_price=None):
    store.add_decision(ticker=ticker, title="t", bid=ask - 0.01, ask=ask, model_p=0.8, edge=0.0,
                       action="bet", mode="observe", lane=lane, side=side, stake_usd=2.0, contracts=2.0 / ask)
    store.db.execute("UPDATE decisions SET ts=? WHERE ticker=?", (when, ticker))
    if exit_price is not None:
        store.mark_exited(ticker, exit_price, "stop-loss: test", lane)
    store.set_outcome(ticker, outcome)
    store.db.commit()


def test_family_is_read_from_the_15_minute_series():
    assert clock.family_of("KXBTC15M-1") == "crypto"
    assert clock.family_of("KXEURUSD15M-1") == "fx"
    assert clock.family_of("KXGOLD15M-1") == "commodity"


def test_this_hour_is_compared_to_the_same_clock_hour_last_week(tmp_path):
    store = Store(tmp_path / "m.db")
    now = ts(2026, 10, 26, 12, 45)  # Monday 12:45 ET
    # last week's Monday noon: 4 wins
    for i in range(4):
        bet(store, f"KXBTC15M-W{i}", "crypto_favorite", 0.80, "yes", "yes", ts(2026, 10, 19, 12, 10 + i))
    # this hour: 2 losses
    bet(store, "KXBTC15M-A", "crypto_favorite", 0.80, "yes", "no", ts(2026, 10, 26, 12, 10))
    bet(store, "KXBTC15M-B", "crypto_favorite", 0.80, "yes", "no", ts(2026, 10, 26, 12, 20))
    # a different hour this week should not pollute the same-hour baseline
    bet(store, "KXBTC15M-Z", "crypto_favorite", 0.80, "yes", "yes", ts(2026, 10, 26, 9, 0))
    snap = clock.snapshot(store, now, hours=1)
    assert snap["hour_et"] == 12 and snap["dow"] == 0  # Monday
    assert snap["this"]["n"] == 2 and snap["this"]["win"] == 0.0
    assert snap["same_hour_7d"]["n"] == 4 and snap["same_hour_7d"]["win"] == 1.0
    assert snap["families"]["crypto"]["n"] == 2
    text = clock.report(store, snap)
    assert "Mon 12:00 ET" in text and "this hour: 2 bets, 0% won" in text
    assert "same clock hour, last 7 days" in text and "4 bets, 100% won" in text
    assert "*12" in text  # the 24h profile marks this hour


def test_a_slice_is_saved_so_weekday_hours_can_accumulate(tmp_path):
    store = Store(tmp_path / "m.db")
    now = ts(2026, 10, 26, 16, 30)
    bet(store, "KXEURUSD15M-1", "fx_favorite", 0.80, "yes", "no", ts(2026, 10, 26, 16, 5))
    snap = clock.snapshot(store, now, hours=1)
    clock.record(store, snap)
    clock.record(store, snap)  # same hour_end: replace, do not double
    rec = clock.recorded_weekday(store, 16, 0, "fx")
    assert rec["hours"] == 1 and rec["n"] == 1 and rec["win"] == 0.0
