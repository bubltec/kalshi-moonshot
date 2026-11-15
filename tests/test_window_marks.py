"""The window offset is learned from what the stream already says (a window's strike as it opens, its result as it settles), kept in window_marks, not from a REST read of settled markets (2026-11-14: "the window offsets are still read over REST").
Unit: the store's marks and the listing it answers with. Integration: lifecycle events through the worker's note, the store as the learner's source, the offset it learns, and the lane's scope run with a client that refuses the REST read."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from kalshi_moonshot import commodities as cm
from kalshi_moonshot import lanes, runner
from kalshi_moonshot.store import Store
from kalshi_moonshot.worker import Worker
from tests.test_commodities import NOW, WindowFeed, boundary_world, feed_series, settings

SERIES = "KXSILVER15M"
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def ticker_at(close: datetime, series: str = SERIES) -> str:
    et = close.astimezone(ZoneInfo("America/New_York"))
    return f"{series}-{et:%y}{MONTHS[et.month - 1]}{et:%d%H%M}-00"


def stream_it(store: Store, markets: list[dict], with_results: bool = True) -> None:
    """Feed a settled-window list through the worker's note, as the stream would have: a strike event as each opened, a result event as each closed."""
    for m in markets:
        close = datetime.fromisoformat(m["close_time"])
        t = ticker_at(close)
        Worker._note_window(store, {"event": "strike", "kind": "strike", "ticker": t, "strike": m["floor_strike"]})
        if with_results:
            Worker._note_window(store, {"event": "determined", "kind": "result", "ticker": t, "result": "yes"})


def test_a_strike_and_a_result_arriving_apart_fill_one_row(tmp_path):
    store = Store(tmp_path / "m.db")
    close = int(NOW.timestamp())
    store.note_window("KXBTC15M-A", close, strike=100.5)
    store.note_window("KXBTC15M-A", close, result="no")
    store.note_window("KXBTC15M-A", close, strike=None, result=None)  # nothing to keep
    rows = store.db.execute("SELECT strike, result FROM window_marks").fetchall()
    assert len(rows) == 1 and (rows[0]["strike"], rows[0]["result"]) == (100.5, "no")


def test_the_listing_has_the_rest_shape_newest_first_and_the_history_row_wins(tmp_path):
    store = Store(tmp_path / "m.db")
    t0 = int(NOW.timestamp()) // 900 * 900
    for i in range(3):
        store.note_window(f"{SERIES}-{i}", t0 + i * 900, strike=50.0 + i, result="yes")
    store.db.execute(
        "INSERT INTO windows (ticker, series, close_ts, strike, result, fetched_ts, settled_value) VALUES (?,?,?,?,?,?,?)",
        (f"{SERIES}-1", SERIES, t0 + 900, 51.0, "no", 1.0, 51.25),
    )
    store.db.commit()
    store.note_window(f"{SERIES}-nostrike", t0 + 5000, result="yes")  # a result with no strike teaches no reference
    got = store.settled_markets(SERIES, limit=2)
    assert [m["ticker"] for m in got] == [f"{SERIES}-2", f"{SERIES}-1"]
    assert got[1]["result"] == "no" and got[1]["expiration_value"] == 51.25  # the history row, with its settled value
    assert datetime.fromisoformat(got[0]["close_time"]).timestamp() == t0 + 1800
    assert store.settled_markets("KXOTHER15M") == []


def test_only_15_minute_strikes_and_results_with_a_close_in_the_ticker_are_kept(tmp_path):
    store = Store(tmp_path / "m.db")
    Worker._note_window(store, {"event": "strike", "ticker": "KXGOLDD-26OCT191100-00", "strike": 1.0})  # not a 15-minute series
    Worker._note_window(store, {"event": "strike", "ticker": f"{SERIES}-nodate", "strike": 1.0})
    Worker._note_window(store, {"event": "created", "ticker": ticker_at(NOW), "strike": 1.0})
    assert store.db.execute("SELECT COUNT(*) FROM window_marks").fetchone()[0] == 0
    Worker._note_window(store, {"event": "strike", "ticker": ticker_at(NOW), "strike": "57.12"})
    row = store.db.execute("SELECT close_ts, strike FROM window_marks").fetchone()
    assert row["close_ts"] == int(NOW.timestamp()) and row["strike"] == pytest.approx(57.12)


def test_the_offset_learned_from_the_streams_marks_equals_the_one_from_the_rest_listing(tmp_path):
    s = settings(tmp_path)
    markets, minutes = boundary_world(offset=-0.0003, n=10)
    rest = cm.CommodityPricer(WindowFeed(feed_series(price=57.0, age_s=3), minutes))

    class Rest:
        def settled_markets(self, series, limit=40):
            return markets if series == SERIES else []

    assert rest.learn_windows(Rest(), s, NOW) == 1
    store = Store(tmp_path / "m.db")
    stream_it(store, markets)  # strikes and results only: no expiration value
    mine = cm.CommodityPricer(WindowFeed(feed_series(price=57.0, age_s=3), minutes))
    assert mine.learn_windows(store, s, NOW) == 1
    a, b = rest.windows[SERIES], mine.windows[SERIES]
    assert b.basis == pytest.approx(a.basis, abs=2e-5) and b.n >= 6  # the next window's strike is the last one's settled value


def test_a_new_start_learns_from_the_history_rows_before_any_stream_event(tmp_path):
    s = settings(tmp_path)
    markets, minutes = boundary_world(offset=-0.0003, n=10)
    store = Store(tmp_path / "m.db")
    for m in markets:
        close = int(datetime.fromisoformat(m["close_time"]).timestamp())
        store.db.execute(
            "INSERT INTO windows (ticker, series, close_ts, strike, result, fetched_ts, settled_value) VALUES (?,?,?,?,?,?,?)",
            (ticker_at(datetime.fromtimestamp(close, UTC)), SERIES, close, m["floor_strike"], "yes", 1.0, float(m["expiration_value"])),
        )
    store.db.commit()
    pricer = cm.CommodityPricer(WindowFeed(feed_series(price=57.0, age_s=3), minutes))
    assert pricer.learn_windows(store, s, NOW) == 1


def test_a_lane_scope_reads_no_settled_markets_from_kalshi(tmp_path, monkeypatch):
    from tests.test_event_driven_lanes import make

    s, w, client, _ = make(tmp_path)
    seen = []
    quant = w.quant
    monkeypatch.setattr(quant, "learn_windows", lambda source, *a, **k: seen.append(source) or 0)
    store = Store(s.db_path)
    runner.scope_lane(s, client, store, lanes.lane_specs(s)["commodity"], quant=quant)
    assert seen and all(src is store for src in seen) and src_is_not_client(seen, client)


def src_is_not_client(seen, client):
    return all(src is not client for src in seen)
