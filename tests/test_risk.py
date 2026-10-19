import time

import pytest

from kalshi_moonshot import config, risk
from kalshi_moonshot.store import Store


@pytest.fixture
def s(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODE", "trade")
    monkeypatch.setenv("KALSHI_ENV", "demo")
    return config.load()


@pytest.fixture
def store(s):
    return Store(s.db_path)


def order(store, ticker, cost, env="demo", fill=1.0):
    store.add_order(ticker=ticker, env=env, count=1, price=0.05, fill_count=fill,
                    cost_usd=cost, fee_usd=0.0, order_id="x")


def test_allows_a_normal_trade(s, store):
    assert risk.check(s, store, "A", 4.0).ok


def test_observe_mode_never_trades(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODE", "observe")
    s = config.load()
    assert not risk.check(s, Store(s.db_path), "A", 1.0).ok


def test_prod_needs_the_explicit_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODE", "trade")
    monkeypatch.setenv("KALSHI_ENV", "prod")
    monkeypatch.delenv("ALLOW_REAL_MONEY", raising=False)
    s = config.load()
    v = risk.check(s, Store(s.db_path), "A", 1.0)
    assert not v.ok and "ALLOW_REAL_MONEY" in v.reason


def test_kill_file_blocks(s, store):
    s.kill_file.write_text("x")
    assert not risk.check(s, store, "A", 1.0).ok


def test_per_trade_and_daily_caps(s, store):
    assert not risk.check(s, store, "A", s.max_usd_per_trade + 1).ok
    for i in range(4):
        order(store, f"T{i}", 5.0)
    v = risk.check(s, store, "NEW", 5.0)
    assert not v.ok and "daily cap" in v.reason


def test_yesterdays_spend_does_not_count(s, store):
    store.add_order(ticker="OLD", env="demo", count=1, price=0.05, fill_count=1.0,
                    cost_usd=20.0, fee_usd=0.0, order_id="x", ts=time.time() - 3 * 86_400)
    assert risk.check(s, store, "A", 4.0).ok


def test_no_duplicate_position(s, store):
    order(store, "A", 2.0)
    assert not risk.check(s, store, "A", 2.0).ok


def test_mutex_event_blocks_the_other_side(s, store):
    store.add_decision(ticker="PTK", action="bet", mode="trade", event_ticker="GAME")
    v = risk.check(s, store, "FEN", 1.0, event_ticker="GAME", mutually_exclusive=True)
    assert not v.ok and "another side" in v.reason
    assert risk.check(s, store, "UNRELATED", 1.0, event_ticker="OTHER", mutually_exclusive=True).ok
