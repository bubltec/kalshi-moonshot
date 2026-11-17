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
    store.rebuild_book()  # the bot builds the bet book when it starts, before anything is evaluated
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


def test_integration_settling_a_market_reads_that_market_only(tmp_path):
    store = Store(tmp_path / "m.db")
    for k in range(5):
        store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, model_p, outcome, title) VALUES (?,?,?,?,?,?,?,?,?,?)", (float(k), f"M-{k}", "bet", "paper", 0.6, "yes", "x", 0.7, "yes", "t"))
    store.db.commit()
    assert [r["ticker"] for r in store.settled_decisions(tickers=["M-3"])] == ["M-3"]
    assert store.settled_decisions(tickers=[]) == [] and len(store.settled_decisions()) == 5
    seen = spy(store)
    store.rag.db = store.db
    store.rag.index_settled(["M-3"])
    reads = [q for q in seen if q.lstrip().upper().startswith("SELECT") and "FROM decisions" in q]
    assert reads and all("ticker IN" in q for q in reads)


# The bet book (2026-11-17, user: "you shouldn't be using the db. everything in current scope for a buy/sell judgement comes from memory").
def scenario(store):
    """Bets in every state the four reads tell apart: open, open in a second lane, open with no stake, sold and unsettled, sold and settled a moment ago, settled long ago, another event."""
    now = time.time()
    rows = [
        # ts, ticker, lane, stake, outcome, exit_ts, event
        (now - 500, "E1-A", "x", 1.0, None, None, "E1"),
        (now - 480, "E1-A", "x", 1.0, None, None, "E1"),      # a second bet of the same lane and market: the first is the one listed
        (now - 470, "E1-A", "y", 2.0, None, None, "E1"),      # another lane holds the same market
        (now - 460, "E1-B", None, None, None, None, "E1"),    # no lane (the default one) and no stake
        (now - 450, "E1-C", "x", 1.0, None, now - 100, "E1"),  # sold, not settled
        (now - 440, "E2-A", "x", 1.0, "yes", now - 50, "E2"),  # sold and settled inside the hour
        (now - 9000, "E3-A", "x", 1.0, "no", now - 8000, "E3"),  # sold and settled long ago
        (now - 430, "E2-B", "y", 1.0, "no", None, "E2"),      # held and settled
    ]
    for ts, tk, lane, stake, outcome, exit_ts, ev in rows:
        store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, outcome, exit_ts, event_ticker, close_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (ts, tk, "bet", "paper", 0.6, "yes", lane, 1.0, stake, outcome, exit_ts, ev, "2099-01-01T00:00:00Z"))
    store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, lane, event_ticker) VALUES (?,?,?,?,?,?)", (now - 10, "E1-D", "pass", "observe", "x", "E1"))  # a pass is no bet
    store.db.commit()
    return now


def answers(store):
    out = {"open": [(r["ticker"], r["lane"], r["id"]) for r in store.open_bets()]}
    for tk in ("E1-A", "E1-B", "E1-C", "E2-A", "E3-A", "E1-D", "NONE"):
        for lane in ("x", "y", "moonshot", None):
            out[("holds", tk, lane)] = store.holds_sized_bet(tk, lane)
            if lane is not None:
                out[("exit", tk, lane)] = store._last_exit(tk, lane)
                out[("wait", tk, lane)] = round(store.exited_within(tk, lane, 900.0, now=1e10) > 0, 0)
    for ev in ("E1", "E2", "E3", "", "NONE"):
        out[("event", ev)] = store.bet_tickers_in_event(ev)
    return out


def test_unit_the_bet_book_answers_what_the_table_answers(tmp_path):
    store = Store(tmp_path / "m.db")
    scenario(store)
    from_table = answers(store)
    assert from_table["open"] and from_table[("holds", "E1-A", "y")] and not from_table[("holds", "E1-B", "moonshot")] and from_table[("event", "E1")] == {"E1-A", "E1-B", "E1-C"}  # the scenario exercises the reads
    store_mod.serve_from_memory()
    store.rebuild_book()
    seen = spy(store)
    from_book = answers(store)
    assert seen == []  # not one statement
    long_ago = ("exit", "E3-A", "x")  # the one deliberate difference: a bet sold and settled over an hour ago is not in the book (no cooldown reaches that far)
    assert from_table[long_ago] is not None and from_book[long_ago] is None
    assert {k: v for k, v in from_book.items() if k not in (long_ago, ("wait", "E3-A", "x"))} == {k: v for k, v in from_table.items() if k not in (long_ago, ("wait", "E3-A", "x"))}


def test_integration_a_bet_a_sale_and_a_settlement_reach_the_book_at_once_and_readers_never_read(tmp_path):
    writer, reader = Store(tmp_path / "m.db"), Store(tmp_path / "m.db")  # two threads' stores in the running bot
    store_mod.serve_from_memory()
    writer.rebuild_book()
    seen = spy(reader)
    assert reader.open_bets() == [] and not reader.holds_sized_bet("A-1", "x")
    bet(writer, "A-1")
    assert [r["ticker"] for r in reader.open_bets()] == ["A-1"] and reader.holds_sized_bet("A-1", "x")  # exact the moment the write returned
    writer.mark_exited("A-1", 0.4, "loss sale: test", "x")
    assert reader.open_bets() == [] and not reader.holds_sized_bet("A-1", "x") and reader.exited_within("A-1", "x", 900.0) > 0
    assert seen == []  # the reader's connection was never used: the writer rebuilt the book


def test_integration_a_stale_rebuild_does_not_replace_a_newer_book(tmp_path):
    store = Store(tmp_path / "m.db")
    store_mod.serve_from_memory()
    store.rebuild_book()
    bet(store, "A-1")  # generation 1: the book has the bet
    p = str(store.path)
    newer = store_mod._BOOK[p]
    store_mod._GEN[(p, "bets")] -= 1  # a keeper that read its generation before the write, finishing after it
    store.rebuild_book()
    store_mod._GEN[(p, "bets")] += 1
    assert store_mod._BOOK[p] is newer


def test_integration_off_the_table_is_read_as_it_always_was(tmp_path):
    store = Store(tmp_path / "m.db")
    bet(store, "A-1")
    seen = spy(store)
    assert len(store.open_bets()) == 1 and store.holds_sized_bet("A-1", "x")
    assert seen and store_mod._BOOK == {}
