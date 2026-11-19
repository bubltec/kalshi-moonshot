"""What a buy decision reads comes from memory fed by a background job (2026-11-19, CLAUDE.md: no database call in buy or sell logic)."""
import time
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_moonshot import bankroll, config, memguard, risk, runner
from kalshi_moonshot import store as store_mod
from kalshi_moonshot.marketdata import MarketCache
from kalshi_moonshot.store import Store


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(memguard, "raising", True)
    memguard.reset()
    yield
    store_mod._DEC_BG["on"] = False
    store_mod._BACKGROUND["on"] = False
    store_mod._BOOK.clear()
    store_mod._LAST_DEC.clear()
    store_mod._DEC_FROM.clear()
    risk._SERVED.clear()


def settings(tmp_path):
    s = config.load()
    return type(s)(**{**s.__dict__, "data_dir": tmp_path, "mode": "observe", "env": "demo", "paper_bankroll_usd": 100.0})


def bet(store, lane, stake, mode="observe", ticker="KX-1"):
    store.add_decision(ticker=ticker, action="bet", mode=mode, lane=lane, side="yes", contracts=10, stake_usd=stake, bid=0.4, ask=0.5)


def test_the_balance_in_a_decision_comes_from_the_bet_book_and_a_snapshot_not_the_tables(tmp_path):
    s = settings(tmp_path)
    store = Store(s.db_path)
    bet(store, "a", 5.0)
    bet(store, "b", 7.0, ticker="KX-2")
    store.rebuild_book()
    store_mod._BACKGROUND["on"] = True
    store.bankroll.refresh(object(), s)  # the background job: reads the tables
    store.bankroll.served = True
    with memguard.decision("buy"):  # a read of any table here raises
        bal = store.bankroll.read(object(), s, live=False)
    assert bal.open_by_lane == {"a": 5.0, "b": 7.0} and bal.available == pytest.approx(88.0) and bal.source == "paper"
    # the book is current without a refresh: a new bet shows at once, the realized part waits for the next snapshot
    bet(store, "a", 3.0, ticker="KX-3")
    with memguard.decision("buy"):
        assert store.bankroll.read(object(), s, live=False).open_by_lane["a"] == 8.0


def test_a_live_balance_is_the_snapshot_and_a_missing_one_is_cold_memory_not_a_request(tmp_path):
    s = type(settings(tmp_path))(**{**settings(tmp_path).__dict__, "mode": "trade"})
    store = Store(s.db_path)
    store.rebuild_book()
    store_mod._BACKGROUND["on"] = True

    class Kalshi:
        authed = True
        calls = 0

        def balance(self):
            self.calls += 1
            return {"balance_dollars": "40.00", "portfolio_value": 1000}

    k = Kalshi()
    store.bankroll.served = True
    with memguard.decision("buy"), pytest.raises(bankroll.ColdMemory):
        store.bankroll.read(k, s, live=True)
    assert k.calls == 0
    store.bankroll.refresh(k, s)
    with memguard.decision("buy"):
        bal = store.bankroll.read(k, s, live=True)
    assert (bal.available, bal.equity, bal.source) == (40.0, 50.0, "kalshi") and k.calls == 1


def test_todays_realized_result_for_the_loss_limit_is_read_from_memory(tmp_path):
    s = settings(tmp_path)
    store = Store(s.db_path)
    risk.serve(store)
    day = risk.start_of_day()
    with memguard.decision("buy"):
        assert risk.realized_today(store, day) == 0.0  # nothing read yet: no table is asked
    risk.refresh_realized(store)  # the background job
    with memguard.decision("buy"):
        assert risk.realized_today(store, day) == 0.0


def test_the_cooldown_is_answered_from_memory_right_after_a_restart(tmp_path):
    s = settings(tmp_path)
    store = Store(s.db_path)
    store.add_decision(ticker="KX-1", action="pass", mode="observe", lane="a", ts=time.time() - 120)
    store_mod._DEC_BG["on"] = True  # what worker.start turns on
    store_mod._LAST_DEC.clear()  # a restart: nothing remembered
    assert store.preload_decisions() == 1  # the background job
    with memguard.decision("buy"):
        assert store.decided_since("KX-1", time.time() - 600, "a") is True
        assert store.decided_since("KX-1", time.time() - 60, "a") is False
        assert store.decided_since("KX-2", time.time() - 600, "a") is False


def test_the_screen_log_is_written_behind_in_the_running_bot(tmp_path):
    s = settings(tmp_path)
    store = Store(s.db_path)
    store_mod._DEC_BG["on"] = True
    sent = []
    store.behind = lambda fn: sent.append(fn) or True
    with memguard.decision("buy"):
        assert store.log_screened("a", [{"ticker": "KX-1", "side": "yes"}]) == 1
    assert len(sent) == 1
    sent[0](store)  # the write-behind thread runs it
    assert store.db.execute("SELECT count(*) AS n FROM screen_log").fetchone()["n"] == 1


def test_the_market_comes_from_the_streams_copy_and_a_market_with_no_streamed_quote_is_none():
    cache = MarketCache()
    close = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    row = {"ticker": "KXBTC15M-1-T1", "event_ticker": "KXBTC15M-1", "series_ticker": "KXBTC15M", "close_time": close, "status": "active", "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.45"}
    cache.put_series("KXBTC15M", [row])
    assert cache.get_market_streamed("KXBTC15M-1-T1") is None  # the listing alone is not a quote
    cache.note_quote("KXBTC15M-1-T1", 0.52, 0.55)
    cache.feed(True)
    cache.put_series("KXBTC15M", [row])  # read after the subscription: the stream keeps it current
    got = cache.get_market_streamed("KXBTC15M-1-T1")
    assert got["yes_bid_dollars"] == "0.5200" and got["yes_ask_dollars"] == "0.5500"


def test_a_socket_review_never_asks_kalshi(tmp_path):
    from tests.test_two_level import Client, live_event, settings as two_level_settings

    s = two_level_settings(tmp_path)
    store = Store(s.db_path)
    from kalshi_moonshot import lanes

    spec = lanes.spec_for(s, "commodity")
    runner.scope_lane(s, Client(live_event()), store, spec)

    class Memory(Client):
        def event_markets(self, event):
            raise AssertionError("a request")

        def event_markets_memory(self, event):
            return [dict(m) for m in self.markets if m["event_ticker"] == event]

        def event_info_memory(self):
            return {}

        def candlesticks(self, *a, **k):
            raise AssertionError("a request")

    from kalshi_moonshot.estimators import build_estimators
    from tests.test_two_level import live_quant

    shots = runner.review_lane(s, Memory(live_event()), store, spec, build_estimators(None, store, None, live_quant()), memory=True)
    assert len(shots) == 1


def test_tape_readers_come_from_a_small_pool_not_one_connection_per_thread(tmp_path):
    """2026-11-19 21:53: 54 of the server's 60 clients were idle connections held by threads that had read the tape once."""
    import threading

    from kalshi_moonshot import db

    got = []

    def grab():
        got.append(db.reader(tmp_path / "tape.db"))

    threads = [threading.Thread(target=grab) for _ in range(50)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len({id(c) for c in got}) <= db.READER_POOL


def test_a_bets_row_is_written_at_once_and_its_snapshot_is_filled_in_behind(tmp_path):
    s = settings(tmp_path)
    store = Store(s.db_path)
    store_mod._DEC_BG["on"] = True
    sent = []
    store.behind = lambda fn: sent.append(fn) or True
    with memguard.decision("buy"):  # the snapshot's reads would raise here
        rid = store.add_decision(ticker="KX-1", action="bet", mode="observe", lane="a", side="yes", stake_usd=1.0, contracts=2, _factors_job=lambda ts: '{"spread": 0.02}')
    assert rid > 0 and store.db.execute("SELECT factors FROM decisions WHERE id=?", (rid,)).fetchone()["factors"] in (None, "")
    sent[0](store)  # the write-behind thread
    assert '"spread": 0.02' in store.db.execute("SELECT factors FROM decisions WHERE id=?", (rid,)).fetchone()["factors"]


def test_registering_a_new_position_with_the_sell_watcher_does_not_hold_the_buy_thread(tmp_path):
    import threading

    from kalshi_moonshot import worker

    class Rec:
        def follow(self, t):
            pass

    gate, ran = threading.Event(), []

    class Watcher:
        def refresh(self):
            gate.wait(3.0)
            ran.append(1)

    w = worker.Worker.__new__(worker.Worker)
    w.watcher, w.recorder, w.stopping = Watcher(), Rec(), False
    t0 = __import__("time").time()
    w._watch_new_position("KX-1")  # returns while refresh is still blocked
    assert __import__("time").time() - t0 < 1.0 and not ran
    gate.set()
    for _ in range(100):
        if ran:
            break
        __import__("time").sleep(0.02)
    assert ran
    w.stopping = True


def test_bets_that_land_together_rebuild_the_book_once_each_round_and_every_bet_is_in_it(tmp_path):
    import threading

    s = settings(tmp_path)
    store = Store(s.db_path, shared=True)
    store.rebuild_book()
    store_mod._BACKGROUND["on"] = True
    store_mod._BOOK_SEQ.clear()
    store_mod._BOOK_COVERED.clear()
    reads = []
    real = store.rebuild_book

    def counted():
        reads.append(1)
        return real()

    store.rebuild_book = counted
    for i in range(12):  # the rows are in (a Postgres connection serialises its writers; SQLite's shared one does not, so they go in one by one here)
        store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, lane, side, contracts, stake_usd, ask) VALUES (?,?,?,?,?,?,?,?,?)", (time.time(), f"KX-{i}", "bet", "observe", "a", "yes", 10, 1.0, 0.5))
    store.db.commit()
    threads = [threading.Thread(target=lambda: store.touch("bets")) for _ in range(12)]  # twelve bets' touches at once
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(store._book()) == 12  # every bet is in the book once all have returned
    assert len(reads) < 12


def test_warming_builds_the_feeds_bars_for_every_instrument_before_the_first_buy(tmp_path):
    from kalshi_moonshot import commodities as cm
    from kalshi_moonshot.config import load

    s = type(load())(**{**load().__dict__, "data_dir": tmp_path})
    asked = []

    class Feed:
        def series(self, symbol):
            asked.append(symbol)
            if symbol.endswith("0"):
                raise cm.CommodityError("no ticks")  # one instrument with no ticks does not stop the rest
            return None

    n = cm.CommodityPricer(Feed()).warm(s)
    assert asked and n == len([x for x in asked if not x.endswith("0")])
