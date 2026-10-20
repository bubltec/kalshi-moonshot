from datetime import UTC, datetime, timedelta

import pytest

from kalshi_moonshot import journal
from kalshi_moonshot.store import Store


def bet(store, ticker, lane, side, outcome, stake, contracts, closes, mode="observe", exit_price=None):
    store.add_decision(
        ticker=ticker, title="t", bid=0.5, ask=0.5, model_p=0.6, edge=0.1, action="bet", mode=mode,
        lane=lane, side=side, contracts=contracts, stake_usd=stake,
        close_time=closes.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    if exit_price is not None:
        store.mark_exited(ticker, exit_price, "stop")
    elif outcome:
        store.set_outcome(ticker, outcome)


def test_each_finished_bet_becomes_a_dollar_trade(tmp_path):
    store = Store(tmp_path / "m.db")
    t0 = datetime(2026, 10, 19, 18, 0, tzinfo=UTC)
    bet(store, "KXA-1", "commodity", "yes", "yes", 4.0, 5, t0)  # wins: 5 contracts pay $5
    bet(store, "KXA-2", "commodity", "no", "yes", 4.0, 5, t0)  # NO bought, market went YES: loses
    bet(store, "KXA-3", "commodity", "yes", None, 4.0, 5, t0)  # still open: not a trade
    items = journal.trades(store)
    assert [(t.ticker, round(t.pnl, 2)) for t in items] == [("KXA-1", 1.0), ("KXA-2", -4.0)]


def test_a_stopped_out_bet_returns_the_exit_price_minus_the_fee(tmp_path):
    store = Store(tmp_path / "m.db")
    bet(store, "KXS-1", "commodity", "yes", None, 4.0, 5, datetime.now(UTC) + timedelta(hours=1), exit_price=0.50)
    (t,) = journal.trades(store)
    assert t.stopped and t.returned == pytest.approx(5 * (0.50 - 0.07 * 0.25))


def mk(pnl, day, stake=4.0, live=True, lane="commodity"):
    ts = datetime(2026, 10, day, 18, 0, tzinfo=UTC).timestamp()
    return journal.Trade("T", lane, "KXA", "yes", live, stake, stake + pnl, ts)


def test_days_use_new_york_dates_not_utc():
    late = datetime(2026, 10, 20, 2, 0, tzinfo=UTC).timestamp()  # 10pm on the 19th in New York
    assert journal.day_of(late) == "2026-10-19"
    assert journal.day_of(late, "UTC") == "2026-10-20"


def test_daily_rows_and_the_summary():
    items = [mk(+0.5, 19), mk(+0.5, 19), mk(-4.0, 19), mk(+0.5, 20), mk(+0.5, 20)]
    days = journal.by_day(items)
    assert [(d.day, d.n, d.wins, round(d.pnl, 2)) for d in days] == [("2026-10-19", 3, 2, -3.0), ("2026-10-20", 2, 2, 1.0)]
    m = journal.summarize(items)
    assert (m.trades, m.wins, m.days, m.green_days) == (5, 4, 2, 1)
    assert m.pnl == pytest.approx(-2.0)
    assert m.profit_factor == pytest.approx(2.0 / 4.0)
    assert m.max_drawdown == pytest.approx(4.0) and m.worst_streak == 1  # from +1.0 down to -3.0
    assert m.payoff == pytest.approx(0.5 / 4.0)
    assert m.breakeven_win_rate == pytest.approx(1 / (1 + 0.125))  # small wins need a very high hit rate


def test_filters():
    items = [mk(1, 19, live=True), mk(1, 19, live=False), mk(1, 19, lane="moonshot")]
    assert len(journal.select(items, live=True)) == 2
    assert len(journal.select(items, lane="moonshot")) == 1
    assert len(journal.select(items, live=False, lane="commodity")) == 1


def test_an_empty_book_summarizes_without_crashing():
    m = journal.summarize([])
    assert m.trades == 0 and m.pnl == 0.0
