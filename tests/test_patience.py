import pytest

from kalshi_moonshot import db as database
from kalshi_moonshot import patience
from kalshi_moonshot.store import Store


def bet(path, won=True, ask=0.80):
    return patience.Bet("KXBTC15M-1", "yes", ask, won, tuple(path))


def test_buying_at_the_ask_fills_every_bet():
    r = patience.evaluate([bet([]), bet([])], 0.0, 0.0)
    assert r.filled == 2 and r.placed == 2


def test_a_limit_fills_only_if_the_price_reaches_it_in_time():
    quick = bet([(5.0, 0.78)])
    late = bet([(30.0, 0.78)])
    never = bet([(5.0, 0.79)])
    r = patience.evaluate([quick, late, never], 0.02, 10.0)
    assert r.filled == 1 and r.placed == 3


def test_fills_and_misses_are_scored_separately():
    filled_loser = bet([(5.0, 0.78)], won=False)
    missed_winner = bet([(5.0, 0.80)], won=True)
    r = patience.evaluate([filled_loser, missed_winner], 0.02, 10.0)
    assert r.won_filled == 0.0 and r.won_missed == 1.0


def test_load_reads_quotes_after_the_decision(tmp_path):
    store = Store(tmp_path / "t.db")
    store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, outcome, side, lane) VALUES (?,?,?,?,?,?,?,?)",
                     (1000.0, "KXBTC15M-1", "bet", "trade", 0.80, "yes", "yes", "commodity"))
    store.db.commit()
    tape = database.connect(tmp_path / "tape.db", "tape", shared=True)
    tape.executescript("CREATE TABLE quotes (ts REAL, ticker TEXT, bid REAL, ask REAL, last REAL, sts INTEGER);")
    tape.executemany("INSERT INTO quotes VALUES (?,?,?,?,?,?)", [
        (999.0, "KXBTC15M-1", 0.78, 0.80, None, None), (1004.0, "KXBTC15M-1", 0.76, 0.78, None, None)])
    tape.commit()
    tape.close()
    [b] = patience.load(store, tmp_path / "tape.db")
    assert b.won and b.path == ((pytest.approx(4.0), 0.78),)
    assert "too few" in patience.render([b])
