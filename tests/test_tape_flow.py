import sqlite3

from kalshi_moonshot.kalshiws import parse_message
from kalshi_moonshot.tape import Tape


def test_a_ticker_message_carries_the_top_of_book_sizes():
    e = parse_message({"type": "ticker", "msg": {"market_ticker": "KXGOLD15M-1", "yes_bid_dollars": "0.56", "yes_ask_dollars": "0.58",
                                                "price_dollars": "0.57", "yes_bid_size_fp": "1512.11", "yes_ask_size_fp": "3361.12"}})
    assert e is not None and e["bid_size"] == 1512.11 and e["ask_size"] == 3361.12


def test_a_trade_message_says_which_outcome_the_taker_bought():
    msg = {"type": "trade", "msg": {"market_ticker": "KXGOLD15M-1", "yes_price_dollars": "0.5700", "no_price_dollars": "0.4300",
                                    "count_fp": "11.18", "taker_side": "no", "taker_outcome_side": "no", "ts": 1792681392}}
    e = parse_message(msg)
    assert e == {"kind": "trade", "ts": e["ts"], "ticker": "KXGOLD15M-1", "price": 0.57, "count": 11.18, "taker": "no"}
    assert parse_message({"type": "trade", "msg": {"market_ticker": "X", "yes_price_dollars": "0.5", "count_fp": "0"}}) is None


def test_trades_are_summed_per_second_per_market(tmp_path):
    tape = Tape(tmp_path / "t.db")
    for ts, taker, price, count in ((100.2, "yes", 0.60, 10.0), (100.7, "yes", 0.62, 10.0), (100.9, "no", 0.60, 5.0), (101.1, "no", 0.58, 4.0)):
        tape.add({"kind": "trade", "ts": ts, "ticker": "A", "price": price, "count": count, "taker": taker})
    tape.flush()
    rows = tape.db.execute("SELECT ts, ticker, yes_vol, no_vol, n, vwap FROM flow_1s ORDER BY ts").fetchall()
    assert rows[0][:5] == (100, "A", 20.0, 5.0, 3) and abs(rows[0][5] - (0.6 * 10 + 0.62 * 10 + 0.6 * 5) / 25) < 1e-9
    assert rows[1][:5] == (101, "A", 0.0, 4.0, 1)


def test_a_quote_whose_sizes_changed_is_kept_and_an_identical_one_is_not(tmp_path):
    tape = Tape(tmp_path / "t.db")
    base = {"kind": "quote", "ts": 1.0, "ticker": "A", "bid": 0.5, "ask": 0.52, "bid_size": 100.0, "ask_size": 200.0}
    tape.add(base)
    tape.add({**base, "ts": 2.0})  # nothing changed
    tape.add({**base, "ts": 3.0, "bid_size": 300.0})  # same price, a deeper bid
    tape.flush()
    assert tape.db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 2


def test_an_older_tape_file_gets_the_size_columns(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE quotes (ts REAL NOT NULL, ticker TEXT NOT NULL, bid REAL, ask REAL, last REAL, sts INTEGER)")
    old.commit()
    old.close()
    tape = Tape(path)
    assert {"bid_size", "ask_size"} <= {r[1] for r in tape.db.execute("PRAGMA table_info(quotes)")}
