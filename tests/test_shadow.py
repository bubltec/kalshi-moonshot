from datetime import UTC, datetime

import pytest

from kalshi_moonshot import commodities as cm
from kalshi_moonshot import config, lanes, runner, shadow
from kalshi_moonshot.estimators import build_estimators
from kalshi_moonshot.store import Store
from tests.test_commodities import FakeFeed, feed_series, up_down


def settings(tmp_path, **over):
    s = config.load()
    base = {"data_dir": tmp_path, "mode": "observe", "env": "demo", "skip_categories": frozenset(),
            "commodities_enabled": True, "commodity_min_volume": 0.0}
    return type(s)(**{**s.__dict__, **base, **over})


def pricer(now):
    return cm.CommodityPricer(FakeFeed(feed_series(price=82000.0, per_bar=0.0004, age_s=5, now=now)))


def test_snapshot_records_15_minute_markets_only_and_not_twice_in_a_row(tmp_path):
    now = datetime.now(UTC)
    s = settings(tmp_path)
    store = Store(s.db_path)
    fifteen = up_down(82500, minutes=3.0, bid=0.02, ask=0.03, now=now)
    daily = up_down(82500, series="KXBTCD", minutes=3.0, now=now)
    assert shadow.snapshot(store, s, fifteen + daily, "15M", pricer(now), now) == 1
    assert shadow.snapshot(store, s, fifteen, "15M", pricer(now), now) == 0  # too soon
    row = store.shadow_rows(settled_only=False)[0]
    assert row["series"] == "KXBTC15M" and row["yes_ask"] == 0.03
    assert 2.9 < row["minutes_left"] < 3.1
    assert row["model_p"] is not None and row["model_p"] < 0.1  # 0.6% above the price in 3 minutes


def test_an_unpriceable_market_is_still_logged_with_its_book(tmp_path):
    now = datetime.now(UTC)
    s = settings(tmp_path)
    store = Store(s.db_path)
    stale = cm.CommodityPricer(FakeFeed(feed_series(price=82000.0, age_s=600, now=now)))
    shadow.snapshot(store, s, up_down(82500, now=now), "15M", stale, now)
    row = store.shadow_rows(settled_only=False)[0]
    assert row["model_p"] is None and "unpriced" in row["note"] and row["yes_bid"] == 0.05


def test_outcomes_settle_the_shadow_rows(tmp_path):
    now = datetime.now(UTC)
    s = settings(tmp_path)
    store = Store(s.db_path)
    m = up_down(82500, now=now)
    shadow.snapshot(store, s, m, "15M", None, now)
    assert store.unresolved_tickers() == [m[0]["ticker"]]  # watched markets get resolved too
    store.set_outcome(m[0]["ticker"], "no")
    assert store.shadow_rows()[0]["outcome"] == "no" and store.unresolved_tickers() == []


def row(ticker, minutes, bid, ask, outcome, model=None):
    return {"ticker": ticker, "minutes_left": minutes, "yes_bid": bid, "yes_ask": ask,
            "outcome": outcome, "model_p": model}


def test_table_shows_whether_cheap_yes_beats_its_price():
    # 40 markets quoted 2c with 3 minutes left; 4 of them (10%) actually went YES
    rows = [row(f"M{i}", 3.0, 0.01, 0.02, "yes" if i < 4 else "no", model=0.08) for i in range(40)]
    (cell,) = shadow.shadow_table(rows)
    assert (cell.minutes, cell.price, cell.n) == ((2.0, 4.0), (0.0, 0.03), 40)
    assert cell.yes_rate == pytest.approx(0.10) and not cell.thin
    assert cell.model == pytest.approx(0.08)
    assert cell.roi_yes > 3.0  # paying 2c for a 10% shot returns about +400%
    assert cell.roi_no < 0  # the 98c side loses on a 10% rate


def test_table_counts_each_market_once_per_time_bucket_and_flags_thin_cells():
    rows = [row("A", 3.5, 0.50, 0.52, "yes"), row("A", 2.2, 0.52, 0.54, "yes"),  # same bucket: latest wins
            row("A", 9.0, 0.50, 0.52, "yes")]  # a different bucket
    cells = shadow.shadow_table(rows)
    assert sorted(c.minutes for c in cells) == [(2.0, 4.0), (7.0, 11.0)]
    two_four = next(c for c in cells if c.minutes == (2.0, 4.0))
    assert two_four.n == 1 and two_four.market == pytest.approx(0.53) and two_four.thin


def test_the_review_loop_records_the_watched_15_minute_window(tmp_path):
    now = datetime.now(UTC)
    s = settings(tmp_path)
    store = Store(s.db_path)
    spec = lanes.spec_for(s, "commodity")
    window = up_down(82500, minutes=6.0, bid=0.03, ask=0.04, now=now)

    class Client:
        def event_markets(self, event):
            return [dict(m) for m in window]

        def fill_event_info(self, tickers):
            return {}

        def candlesticks(self, *a, **k):
            return []

    store.set_watch("commodity", {window[0]["event_ticker"]: {
        "close": window[0]["close_time"], "category": "Crypto", "series": "KXBTC15M"}})
    runner.review_lane(s, Client(), store, spec, build_estimators(None, None, None, pricer(now)), now)
    assert len(store.shadow_rows(settled_only=False)) == 1
    assert store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0  # watched, never bet


def test_scoping_keeps_a_15_minute_window_that_is_priced_for_a_bet_or_not(tmp_path):
    """Scope must not drop windows whose decided side is too expensive: they are for measuring."""
    now = datetime.now(UTC)
    s = settings(tmp_path)
    store = Store(s.db_path)
    spec = lanes.spec_for(s, "commodity")
    mid = up_down(82500, minutes=9.0, bid=0.49, ask=0.51, now=now)

    class Client:
        def event_info(self):
            return {}

        def series_markets(self, series):
            return [dict(m) for m in mid] if series == "KXBTC15M" else []

    assert runner.scope_lane(s, Client(), store, spec, now) == 1


# ---- entry window: the 15-minute markets are bought around the 10-minute mark ----
def test_a_15_minute_window_is_bought_only_inside_its_entry_window(tmp_path):
    from kalshi_moonshot import strategy

    now = datetime.now(UTC)
    s = settings(tmp_path, commodity_fast_from_min=8.5, commodity_fast_to_min=12.0,
                 commodity_min_return=0.0, commodity_max_spread=0.5, commodity_min_price=0.0,
                 commodity_max_price=1.0, commodity_min_hours_to_close=0.0)
    spec = lanes.spec_for(s, "commodity")
    assert spec.entry_elapsed == (8.5, 12.0)

    def reason(minutes_left):
        m = up_down(82500, minutes=minutes_left, bid=0.60, ask=0.62, now=now)[0]
        return strategy.lane_reject_reason(m, spec, s, now)

    assert "entry window" in reason(14.0)  # one minute in: too early
    assert "entry window" in reason(8.0)  # seven minutes in
    assert reason(6.0) is None  # nine minutes in
    assert reason(4.0) is None  # eleven minutes in
    assert "entry window" in reason(2.0)  # thirteen minutes in: too late
    assert "1.0 min into a 15-min window" in reason(14.0)


def test_the_entry_window_does_not_touch_other_series(tmp_path):
    from kalshi_moonshot import strategy

    now = datetime.now(UTC)
    s = settings(tmp_path, commodity_min_return=0.0, commodity_max_spread=0.5, commodity_min_price=0.0,
                 commodity_max_price=1.0)
    spec = lanes.spec_for(s, "commodity")
    daily = up_down(82500, series="KXBTCD", minutes=90.0, bid=0.60, ask=0.62, now=now)[0]
    assert strategy.lane_reject_reason(daily, spec, s, now) is None  # hourly/daily are not windowed


def test_the_window_can_be_moved_from_the_environment(monkeypatch):
    monkeypatch.setenv("COMMODITY_15M_FROM_MIN", "3")
    monkeypatch.setenv("COMMODITY_15M_TO_MIN", "6.5")
    s = config.load()
    assert lanes.spec_for(s, "commodity").entry_elapsed == (3.0, 6.5)


def test_a_strategies_file_can_set_the_window(tmp_path):
    (tmp_path / "strategies.toml").write_text("[lane.commodity]\nentry_elapsed = [9, 11]\n")
    assert lanes.spec_for(settings(tmp_path), "commodity").entry_elapsed == (9.0, 11.0)


def test_a_daily_market_for_tomorrow_is_not_a_day_trade(tmp_path):
    """The lane day-trades: tomorrow's gold close is 23 hours away and must be refused."""
    from kalshi_moonshot import strategy

    now = datetime.now(UTC)
    s = settings(tmp_path, commodity_min_return=0.0, commodity_max_spread=0.5, commodity_min_price=0.0,
                 commodity_max_price=1.0, commodity_max_hours_to_close=2.0)
    spec = lanes.spec_for(s, "commodity")

    def reason(hours):
        m = up_down(4171, series="KXGOLDD", minutes=hours * 60, bid=0.74, ask=0.76, now=now)[0]
        return strategy.lane_reject_reason(m, spec, s, now)

    assert reason(23.0) == "closes too far out"
    assert reason(1.5) is None  # the same daily market in its last two hours is fine


def test_the_default_horizon_is_two_hours(monkeypatch):
    monkeypatch.delenv("COMMODITY_MAX_HOURS_TO_CLOSE", raising=False)
    assert config.load().commodity_max_hours_to_close == 2.0


def test_a_market_must_be_open_a_while_before_the_lane_bets(tmp_path):
    from datetime import timedelta

    from kalshi_moonshot import strategy

    now = datetime.now(UTC)
    s = settings(tmp_path, commodity_min_return=0.0, commodity_max_spread=0.5, commodity_min_price=0.0,
                 commodity_max_price=1.0, commodity_max_hours_to_close=2.0, commodity_min_age_minutes=20.0)
    spec = lanes.spec_for(s, "commodity")
    assert spec.min_age_minutes == 20.0

    def reason(opened_minutes_ago, series="KXBTC", **extra):
        m = up_down(82050, series=series, minutes=50.0, bid=0.71, ask=0.72, now=now)[0]
        m["open_time"] = (now - timedelta(minutes=opened_minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return strategy.lane_reject_reason(m, spec, s, now)

    assert "opened 2 min ago" in reason(2.3)  # the 21:02 and 19:02 bets
    assert "opened 14 min ago" in reason(14.3)  # the 22:14 bet
    assert reason(25.0) is None
    m = up_down(82050, series="KXBTC", minutes=50.0, bid=0.71, ask=0.72, now=now)[0]
    assert strategy.lane_reject_reason(m, spec, s, now) is None  # no open_time known: not guessed at


def test_the_default_spread_is_tight_and_the_default_wait_is_twenty_minutes(monkeypatch):
    monkeypatch.delenv("COMMODITY_MAX_SPREAD", raising=False)
    monkeypatch.delenv("COMMODITY_MIN_AGE_MINUTES", raising=False)
    s = config.load()
    assert s.commodity_max_spread == 0.02 and s.commodity_min_age_minutes == 20.0


def test_the_age_rule_never_applies_to_15_minute_windows(tmp_path):
    """A 15-minute window is 9 minutes old at its entry point; a 20-minute wait would block it forever."""
    from datetime import timedelta

    from kalshi_moonshot import strategy

    now = datetime.now(UTC)
    s = settings(tmp_path, commodity_min_return=0.0, commodity_max_spread=0.5, commodity_min_price=0.0,
                 commodity_max_price=1.0, commodity_min_hours_to_close=0.0, commodity_min_age_minutes=20.0,
                 commodity_fast_from_min=8.5, commodity_fast_to_min=12.0)
    spec = lanes.spec_for(s, "commodity")

    def reason(series, minutes_left):
        m = up_down(82050, series=series, minutes=minutes_left, bid=0.71, ask=0.72, now=now)[0]
        m["open_time"] = (now - timedelta(minutes=15.0 - minutes_left)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return strategy.lane_reject_reason(m, spec, s, now)

    assert reason("KXBTC15M", 6.0) is None  # 9 minutes in: inside the entry window, age rule ignored
    assert reason("KXBTC15M", 4.0) is None  # 11 minutes in
    assert "entry window" in reason("KXBTC15M", 14.0)  # too early, but for the entry window's reason
    assert "opened" in reason("KXBTC", 6.0)  # an hourly market that young is refused by the age rule
