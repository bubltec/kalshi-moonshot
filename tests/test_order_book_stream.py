"""The order book from Kalshi's orderbook_delta websocket channel (2026-11-16, user: "don't use the rest calls for the order book, or queue")."""
from __future__ import annotations

import types

from kalshi_moonshot import ticks
from kalshi_moonshot.kalshi import KalshiClient
from kalshi_moonshot.kalshiws import KalshiStream, parse_message
from kalshi_moonshot.strategy import best_ask, best_bid


def fresh():
    store = ticks.BookStore()
    store.snapshot("M", [(0.40, 10), (0.38, 5)], [(0.58, 7)], 100.0)
    return store


def test_snapshot_then_deltas_keep_the_book():
    b = fresh()
    assert b.delta("M", "yes", 0.40, -4, 101.0) and b.delta("M", "yes", 0.41, 3, 101.0)
    assert b.levels("M", "yes") == [(0.41, 3), (0.40, 6), (0.38, 5)]
    b.delta("M", "yes", 0.38, -5, 102.0)  # a level that reaches zero is dropped
    assert [p for p, _ in b.levels("M", "yes")] == [0.41, 0.40]
    assert b.delta("OTHER", "yes", 0.5, 1, 1.0) is False  # no snapshot yet: not applied


def test_the_rest_shaped_book_reads_like_the_rest_answer():
    book = fresh().rest_book("M", 150.0)
    assert best_bid(book, "yes") == 0.40 and best_bid(book, "no") == 0.58
    assert best_ask(book, "yes") == 0.42  # the NO bid at 0.58 is a YES ask at 0.42
    assert fresh().rest_book("NONE", 150.0) is None


def test_messages_parse():
    snap = parse_message({"type": "orderbook_snapshot", "sid": 3, "seq": 1, "msg": {"market_ticker": "M", "yes_dollars_fp": [["0.4000", "10.00"]], "no_dollars_fp": [["0.5800", "7.00"]]}})
    assert snap["kind"] == "book_snapshot" and snap["yes"] == [(0.4, 10.0)] and snap["no"] == [(0.58, 7.0)] and snap["seq"] == 1
    d = parse_message({"type": "orderbook_delta", "sid": 3, "seq": 2, "msg": {"market_ticker": "M", "price_dollars": "0.4000", "delta_fp": "-4.00", "side": "yes"}})
    assert d["kind"] == "book_delta" and d["delta"] == -4.0 and d["side"] == "yes" and d["seq"] == 2


def test_a_sequence_gap_drops_the_books_and_ignores_the_delta():
    ticks.books.clear()
    ticks.books.snapshot("M", [(0.4, 1)], [], 1.0)
    seen = []
    s = KalshiStream(types.SimpleNamespace(), seen.append, books=True)
    ev = lambda kind, seq: {"kind": kind, "seq": seq, "sid": 3}
    assert s._book_in_order(ev("book_snapshot", 1)) and s._book_in_order(ev("book_delta", 2))
    assert s._book_in_order(ev("book_delta", 4)) is False
    assert ticks.books.levels("M", "yes") is None  # cleared: fresh snapshots will rebuild it
    ticks.books.clear()


def test_the_client_reads_the_stream_book_and_never_rest(monkeypatch):
    ticks.books.clear()
    ticks.books.snapshot("M", [(0.4, 1)], [(0.58, 1)], 1.0)
    monkeypatch.setattr(ticks.books, "enabled", True)
    c = KalshiClient.__new__(KalshiClient)
    c._request = lambda *a, **k: (_ for _ in ()).throw(AssertionError("REST read"))
    assert best_bid(c.orderbook("M"), "yes") == 0.4
    assert best_ask(c.orderbook("NOPE"), "yes") is None  # no book: empty, nothing to trade off
    ticks.books.clear()


def test_a_jump_names_what_it_skipped(caplog):
    ticks.books.clear()
    s = KalshiStream(types.SimpleNamespace(), lambda e: None, books=True)
    s._book_seq[3] = 1
    s.handle('{"type": "something_else", "sid": 3, "seq": 2, "msg": {"market_ticker": "M"}}')
    with caplog.at_level("WARNING"):
        assert s._book_in_order({"kind": "book_delta", "seq": 4, "sid": 3, "ticker": "X"}) is False
    assert "something_else" in caplog.text and "(3, 'not seen at all')" in caplog.text
    ticks.books.clear()


def test_an_update_subscription_reply_takes_a_number_and_is_no_gap():
    ticks.books.clear()
    ticks.books.snapshot("M", [(0.4, 1)], [], 1.0)
    s = KalshiStream(types.SimpleNamespace(), lambda e: None, books=True)
    s._book_seq[3] = 5
    s.handle('{"type": "ok", "sid": 3, "seq": 6, "msg": {}}')
    assert s._book_in_order({"kind": "book_delta", "seq": 7, "sid": 3, "ticker": "M"}) is True
    assert ticks.books.levels("M", "yes") is not None  # nothing was cleared
    ticks.books.clear()
