"""Remembered reads in the running bot (2026-11-16, user: "Nothing in per tick computations should do a db read. Ever. We can do structured background jobs ... but never per tick")."""
import time

import pytest

from kalshi_moonshot import store as store_mod
from kalshi_moonshot.store import Store


@pytest.fixture(autouse=True)
def off():
    yield
    store_mod.serve_from_memory(False)
    store_mod._MEMO.clear()


def spy(store):
    seen = []
    inner = store.db

    class Watch:
        def execute(_, sql, *args):
            seen.append(sql)
            return inner.execute(sql, *args)

        def __getattr__(_, name):
            return getattr(inner, name)

    store.db = Watch()
    return seen


def bet(store, ticker):
    store.add_decision(ticker=ticker, action="bet", mode="paper", ask=0.6, side="yes", lane="x", contracts=1.0, stake_usd=0.6, close_time="2099-01-01T00:00:00Z")


def test_integration_an_answer_past_its_time_is_served_from_memory_and_re_read_in_the_background(tmp_path, monkeypatch):
    store = Store(tmp_path / "m.db")
    bet(store, "A-1")
    assert len(store.open_bets()) == 1
    monkeypatch.setattr(store_mod, "MEMO_TTL", 0.05)
    store_mod.serve_from_memory()
    time.sleep(0.1)  # the answer's time is up
    seen = spy(store)
    assert len(store.open_bets()) == 1 and seen == []  # the caller did not go to the database
    other = Store(tmp_path / "m.db")  # another process writes without telling this one
    other.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (1,'B-1','bet','paper',0.6,'yes','x',1.0,0.6,'2099-01-01T00:00:00Z')")
    other.db.commit()
    for _ in range(100):  # the refresher picks it up; the caller still never reads
        time.sleep(0.05)
        if len(store.open_bets()) == 2:
            break
    assert len(store.open_bets()) == 2 and seen == []


def test_integration_a_write_in_this_process_is_seen_at_once(tmp_path):
    store = Store(tmp_path / "m.db")
    store_mod.serve_from_memory()
    assert store.open_bets() == []
    bet(store, "A-1")
    assert len(store.open_bets()) == 1  # the book changed here: the next read is fresh, not a remembered one


def test_unit_off_by_default_an_answer_past_its_time_is_read_again(tmp_path, monkeypatch):
    store = Store(tmp_path / "m.db")
    bet(store, "A-1")
    store.open_bets()
    monkeypatch.setattr(store_mod, "MEMO_TTL", 0.05)
    time.sleep(0.1)
    seen = spy(store)
    store.open_bets()
    assert seen != []
