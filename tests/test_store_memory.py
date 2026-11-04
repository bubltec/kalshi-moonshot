import pytest

from kalshi_moonshot import config
from kalshi_moonshot import store as store_mod
from kalshi_moonshot.store import Store


@pytest.fixture
def st(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store_mod, "MEMO_TTL", 60.0)  # the read memory on
    store_mod._MEMO.clear()
    s = config.load()
    store = Store(s.db_path)
    counts = {"n": 0}
    conn = store.db

    class Counting:
        """The same connection, counting the statements sent to it."""

        def __getattr__(self, name):
            return getattr(conn, name)

        def execute(self, sql, params=()):
            counts["n"] += 1
            return conn.execute(sql, params)

    store.db = Counting()
    return store, counts


def _bet(store, lane="a", ticker="KX-1", **kw):
    return store.add_decision(ticker=ticker, title="", bid=0.0, ask=0.8, model_p=0.0, edge=0.0, action="bet", mode="trade", lane=lane, side="yes", contracts=2, stake_usd=1.6, close_time="2099-01-01T00:00:00Z", **kw)


def test_a_repeated_read_is_answered_from_memory_until_a_write_changes_it(st):
    store, counts = st
    _bet(store)
    assert len(store.open_bets()) == 1
    before = counts["n"]
    for _ in range(5):
        assert len(store.open_bets()) == 1 and store.holds_sized_bet("KX-1", "a") and store.exited_within("KX-1", "a", 60.0) == 0.0
    first = counts["n"] - before
    for _ in range(5):
        store.open_bets()
        store.holds_sized_bet("KX-1", "a")
        store.exited_within("KX-1", "a", 60.0)
    assert counts["n"] - before == first  # the second round of reads sent nothing to the database
    _bet(store, ticker="KX-2")  # a new bet is a write: the next read sees it
    assert {r["ticker"] for r in store.open_bets()} == {"KX-1", "KX-2"}


def test_a_sale_and_a_settlement_are_seen_by_the_next_read(st):
    store, _counts = st
    _bet(store)
    assert store.exited_within("KX-1", "a", 60.0) == 0.0 and len(store.open_bets()) == 1
    store.mark_exited("KX-1", 0.5, "stop", "a")
    assert store.open_bets() == [] and not store.holds_sized_bet("KX-1", "a") and store.exited_within("KX-1", "a", 60.0) > 0
    _bet(store, ticker="KX-3")
    assert store.holds_sized_bet("KX-3", "a")
    store.set_outcome("KX-3", "yes")
    assert not store.holds_sized_bet("KX-3", "a")


def test_orders_are_remembered_and_a_new_order_is_seen(st):
    store, _counts = st
    store.add_order(ticker="KX-1", env="demo", count=3, price=0.8, fill_count=3, cost_usd=2.4, fee_usd=0.0, side="buy")
    assert store.net_contracts("KX-1", "demo") == 3 and store.open_position_tickers("demo") == {"KX-1"}
    store.add_order(ticker="KX-1", env="demo", count=3, price=0.9, fill_count=3, cost_usd=2.7, fee_usd=0.0, side="sell")
    assert store.net_contracts("KX-1", "demo") == 0 and store.open_position_tickers("demo") == set()


def test_a_raw_write_is_seen_after_touch_and_after_the_ttl(st, monkeypatch):
    store, _counts = st
    _bet(store)
    assert store.holds_sized_bet("KX-1", "a")
    store.db.execute("UPDATE decisions SET exit_ts=1.0 WHERE ticker='KX-1'")
    store.db.commit()
    assert store.holds_sized_bet("KX-1", "a")  # nobody told the memory
    store.touch("bets")
    assert not store.holds_sized_bet("KX-1", "a")
    store.db.execute("UPDATE decisions SET exit_ts=NULL WHERE ticker='KX-1'")
    store.db.commit()
    assert not store.holds_sized_bet("KX-1", "a")
    monkeypatch.setattr(store_mod, "MEMO_TTL", 0.0)  # an expired entry is read again, which is how another process's write is eventually seen
    assert store.holds_sized_bet("KX-1", "a")


def test_a_caller_cannot_change_the_remembered_answer(st):
    store, _counts = st
    _bet(store)
    store.open_bets().clear()
    assert len(store.open_bets()) == 1


def test_the_regime_rate_never_makes_a_buy_wait_for_a_recompute(monkeypatch):
    import threading
    import time

    from kalshi_moonshot import regime

    regime.reset()
    release, calls = threading.Event(), []

    def slow(s, store, series, hours, now):
        calls.append(threading.current_thread().name)
        release.wait(5.0)
        return (0.9, 40)

    monkeypatch.setattr(regime, "_compute_rate", slow)
    key = (("KXBTC15M",), 6.0)
    regime._rates[key] = (time.time() - 300.0, (0.5, 20))  # older than CACHE_SECONDS, younger than MAX_STALE_SECONDS
    started = time.perf_counter()
    assert regime.favorite_rate(None, None, ("KXBTC15M",), 6.0) == (0.5, 20)  # the remembered answer, at once
    assert time.perf_counter() - started < 0.5
    for _ in range(50):  # a second ask while the refresh runs starts no second one
        if calls:
            break
        time.sleep(0.02)
    regime.favorite_rate(None, None, ("KXBTC15M",), 6.0)
    assert calls == ["regime-refresh"]
    release.set()
    for _ in range(100):
        if not regime._refreshing:
            break
        time.sleep(0.02)
    assert regime.favorite_rate(None, None, ("KXBTC15M",), 6.0) == (0.9, 40)
    regime.reset()
