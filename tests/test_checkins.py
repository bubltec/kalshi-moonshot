from kalshi_moonshot import checkins, config, watcher
from kalshi_moonshot.store import Store


def test_a_mark_is_recorded_once_as_the_clock_passes_it():
    seen: set[int] = set()
    assert checkins.due(300, seen) is None  # before the first mark
    assert checkins.due(239, seen) == 240 and checkins.due(238, seen) is None
    assert checkins.due(209.5, seen) == 210
    assert checkins.due(5, seen) == 10  # the last look
    assert checkins.due(0, seen) is None


def test_a_late_start_or_a_stall_records_nothing_for_marks_it_missed():
    seen: set[int] = set()
    assert checkins.due(100, seen) is None  # a position found at 100s: 240 down to 120 are past, none within 10s of their mark
    assert checkins.due(88, seen) == 90  # but the next mark is still taken on time
    seen = set()
    assert checkins.due(139, seen) is None  # a stalled check: 240..150 missed, 150 is 11s late
    assert checkins.due(121, seen) is None
    assert checkins.due(119, seen) == 120


def test_side_through_is_positive_on_the_losing_side():
    assert checkins.side_through("yes", 100.0, 99.99) > 0 and checkins.side_through("yes", 100.0, 100.5) < 0
    assert checkins.side_through("no", 100.0, 100.01) > 0 and checkins.side_through("no", 100.0, 99.5) < 0
    assert checkins.side_through("yes", 0.0, 99.0) == 0.0


def test_the_watcher_queues_one_check_per_mark_and_the_row_is_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(
        "[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nstrike_through_sale=0.003\nstrike_through_seconds=60\nsettlement_guard=0\nburst_exit=false\nseries=['KXBTC15M']\nalways=true\n"
    )
    s = config.load()
    store = Store(s.db_path)
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1.0, "KXBTC15M-1", "bet", "observe", 0.90, "yes", "guarded", 3, 2.7, "1970-01-24T03:33:20Z"),
    )
    store.db.commit()

    class Client:
        def market(self, ticker):
            return {"ticker": ticker, "floor_strike": 100.0, "close_time": "1970-01-24T03:33:20Z", "yes_bid_dollars": "0.90", "yes_ask_dollars": "0.91", "title": "t"}

    w = watcher.ProfitWatcher(lambda: s, Client(), lambda: Store(s.db_path))
    w.refresh()
    close = 2_000_000.0
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.81, "ask": 0.82, "ts": close - 241})
    w.on_event({"kind": "index", "index_id": "BRTI", "ts": close - 241, "value": 100.2})
    w.check_timers(close - 241)
    assert w._checks.empty()  # 241s left: the first mark is 240
    w.check_timers(close - 239.5)
    t, mark, now, spot = w._checks.get_nowait()
    assert mark == 240 and spot == (close - 241, 100.2)
    w.check_timers(close - 239)
    assert w._checks.empty()  # not twice
    row = checkins.build_row(t, mark, now, spot, '{"x": 1}')
    checkins.record(store, row)
    checkins.record(store, row)  # a repeat is ignored
    saved = store.db.execute("SELECT mark, side, bid, strike, spot, through, left_s, features FROM position_checks").fetchall()
    assert len(saved) == 1 and saved[0]["mark"] == 240 and saved[0]["bid"] == 0.81 and saved[0]["strike"] == 100.0 and saved[0]["spot"] == 100.2
    assert saved[0]["through"] < 0 and abs(saved[0]["left_s"] - 239.5) < 0.01 and saved[0]["features"] == '{"x": 1}'
