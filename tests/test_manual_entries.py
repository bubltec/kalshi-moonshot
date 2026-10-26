import json
import time
from dataclasses import replace

import pytest

from kalshi_moonshot import config, factors, kalshiws, watcher
from kalshi_moonshot.store import Store
from kalshi_moonshot.tape import Tape

TICKER = "KXXRP15M-26OCT241415-15"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = replace(config.load(), manual_exit=True, manual_exit_skip=())
    now = time.time()
    close = now + 600.0  # five minutes into the window
    tape = Tape(tmp_path / "tape.db")
    tape.db.executemany("INSERT INTO quotes (ts, ticker, bid, ask, bid_size, ask_size) VALUES (?,?,?,?,?,?)",
                        [(now - 100, TICKER, 0.60, 0.64, 100, 300), (now - 5, TICKER, 0.66, 0.70, 400, 100)])
    tape.db.commit()
    return s, Store(s.db_path), tape.db, now, close


class Client:
    def __init__(self, close):
        self.close = close

    def market(self, ticker):
        from datetime import UTC, datetime

        return {"close_time": datetime.fromtimestamp(self.close, UTC).isoformat()}


def fill_event(now, order_id="O1", trade_id="T1", side="no", price=0.30):
    return {"kind": "fill", "ticker": TICKER, "side": side, "action": "buy", "count": 10.0, "price": price, "ts": now, "trade_id": trade_id, "order_id": order_id}


def make(world):
    s, _store, tape, _now, close = world
    w = watcher.ProfitWatcher(lambda: s, Client(close), lambda: Store(s.db_path))
    w.refresh = lambda: None  # type: ignore[method-assign]
    w.manual.invalidate = lambda: None  # type: ignore[method-assign]
    w.tape_factory = lambda: tape
    return w


def test_a_fill_message_carries_its_ids():
    msg = {"type": "fill", "msg": {"trade_id": "t9", "order_id": "o9", "market_ticker": TICKER, "side": "yes", "purchased_side": "yes", "action": "buy",
                                   "yes_price_dollars": "0.4", "count_fp": "2.00", "is_taker": True}}
    e = kalshiws.parse_message(msg)
    assert e["trade_id"] == "t9" and e["order_id"] == "o9"


def test_a_hand_placed_buy_is_logged_with_its_context_and_the_bots_own_is_not(world):
    s, store, _tape, now, _close = world
    store.db.execute("INSERT INTO orders (ts, ticker, env, count, price, fill_count, cost_usd, fee_usd, order_id) VALUES (?,?,?,?,?,?,?,?,?)",
                     (now, TICKER, s.env, 1, 0.3, 1, 0.3, 0.01, "BOT"))
    store.db.commit()
    w = make(world)
    w.on_event(fill_event(now - watcher.FILL_SETTLE - 1, order_id="BOT", trade_id="T0"))
    w.on_event(fill_event(now - watcher.FILL_SETTLE - 1, order_id="MINE", trade_id="T1", side="no", price=0.30))
    w.enroll_fills()
    rows = Store(s.db_path).db.execute("SELECT trade_id, side, price, factors, template FROM manual_entries").fetchall()
    assert [r["trade_id"] for r in rows] == ["T1"]  # the bot's own order is not yours
    assert rows[0]["side"] == "no" and rows[0]["price"] == pytest.approx(0.70)  # a fill reports the YES price (0.30); the NO side paid 0.70
    assert rows[0]["template"] == 0 and json.loads(rows[0]["factors"])["imbalance"] is not None


def test_only_marked_buys_are_templates_and_a_mark_can_be_undone(world):
    s, _store, tape, now, _close = world
    w = make(world)
    w.on_event(fill_event(now - watcher.FILL_SETTLE - 1, order_id="MINE", trade_id="T1"))
    w.enroll_fills()
    store = Store(s.db_path)
    assert "0 marked" in factors.manual_report(store)[0]
    assert "marked 1" in factors.mark_template(store, tape, TICKER, "dip bought")
    assert "1 marked" in factors.manual_report(store)[0]
    assert "unmarked 1" in factors.mark_template(store, tape, TICKER, undo=True)
    assert "0 marked" in factors.manual_report(store)[0]


def test_a_buy_from_before_logging_is_rebuilt_from_account_history(world):
    _s, store, tape, now, close = world
    store.db.execute("INSERT INTO fills (fill_id, ts, ticker, book_side, count, yes_price, no_price, order_id, action, side) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     ("F1", now - 3, TICKER, "ask", 10.0, 0.32, 0.68, "MINE", "buy", "no"))
    store.db.execute("INSERT INTO market_info (ticker, series, open_ts, close_ts) VALUES (?,?,?,?)", (TICKER, "KXXRP15M", close - 900, close))
    store.db.commit()
    assert "rebuilt" in factors.mark_template(store, tape, TICKER, "good run")
    row = store.db.execute("SELECT side, price, template, note, factors FROM manual_entries").fetchone()
    assert row["side"] == "no" and row["price"] == pytest.approx(0.68) and row["template"] == 1 and row["note"] == "good run"
    assert json.loads(row["factors"])["elapsed_min"] == pytest.approx(5.0, abs=0.1)
    assert "no hand-placed buy found" in factors.mark_template(store, tape, "KXNOPE-1")


def test_the_profile_needs_three_templates(world):
    _s, store, _tape, now, _close = world
    for i, sp in enumerate((0.02, 0.03, 0.04)):
        store.add_manual_entry(trade_id=f"T{i}", ts=now - i, ticker=f"K{i}", side="yes", price=0.3, count=1.0,
                               factors=json.dumps({"elapsed_min": 5.0 + i, "spread": sp, "imbalance": 0.1, "activity_60": 100.0, "move_90": 0.0, "range_90": 0.1}))
        store.mark_template(f"K{i}", "x")
    out = "\n".join(factors.manual_report(store))
    assert "what your templates have in common" in out and "spread" in out

