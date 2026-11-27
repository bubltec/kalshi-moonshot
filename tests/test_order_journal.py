"""The order journal and its dead-letter retry (orderjournal.py; 2026-11-26, BNB 09:15 YES: filled 16.66 at 0.28, a push restarted the bot 26 s later, the bet's row was never made and nothing sold it)."""

import time

import pytest

from kalshi_moonshot import config, orderjournal
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot

TICKER = "KXBNB15M-26NOV260915-15"


class Client:
    def __init__(self, fills=(), status="executed", fail=0):
        self.fills, self.status, self.fail, self.cancelled = list(fills), status, fail, []

    def portfolio_rows(self, path, key, since, pages=1):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("kalshi is slow")
        return self.fills

    def _request(self, method, path, auth=False, params=None):
        return {"orders": [{"order_id": "oid-1", "status": self.status}]}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {}


def fill(n=16.66, yes=0.28, ts=None, side="yes", ticker=TICKER, fid="f1"):
    return {"fill_id": fid, "market_ticker": ticker, "count_fp": str(n), "yes_price_dollars": str(yes), "no_price_dollars": str(round(1 - yes, 4)), "book_side": "bid", "ts": ts or time.time(), "order_id": "oid-1", "action": "buy", "side": side, "fee_cost": "0", "is_taker": False}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LIVE_LANES", "none")
    s = config.load()
    return s, Store(s.db_path)


def shot(side="yes"):
    return LongShot(ticker=TICKER, title="", rules="", bid=0.27, ask=0.29, volume=0.0, close_time="2026-11-26T14:15:00Z", hours_to_close=0.1, side=side, event_ticker="KXBNB15M-26NOV260915", category="Crypto")


def sent(s, age=1000.0, side="yes"):
    """An intent of an earlier process (older than this one, so it is not inside a live evaluation)."""
    orderjournal.intent(s.data_dir, lane="wave_shape", shot=shot(side), order_id="oid-1", count=16.66, price=0.28, kind="resting")
    lines = orderjournal.path(s.data_dir).read_text().splitlines()
    import json

    row = json.loads(lines[-1])
    row["ts"] = time.time() - age
    lines[-1] = json.dumps(row)
    orderjournal.path(s.data_dir).write_text("\n".join(lines) + "\n")


def bets(store):
    return store.db.execute("SELECT lane, side, contracts, stake_usd, action, mode, event_ticker FROM decisions WHERE ticker=?", (TICKER,)).fetchall()


def test_an_order_is_pending_until_it_is_settled(env):
    s, _ = env
    orderjournal.intent(s.data_dir, lane="wave_shape", shot=shot(), order_id="oid-1", count=16.66, price=0.28, kind="resting")
    [e] = orderjournal.pending(s.data_dir)
    assert e["lane"] == "wave_shape" and e["ticker"] == TICKER and e["side"] == "yes" and e["count"] == 16.66
    orderjournal.settle(s.data_dir, "oid-1", "test")
    assert orderjournal.pending(s.data_dir) == []


def test_an_order_with_no_bet_row_gets_its_row_from_the_fills(env):
    s, store = env
    sent(s)
    out = orderjournal.recover_once(s, Client([fill()]), store)
    assert out and "recovered 16.66" in out[0]
    [row] = bets(store)
    assert row[0] == "wave_shape" and row[1] == "yes" and row[2] == pytest.approx(16.66) and row[3] == pytest.approx(0.28 * 16.66, abs=0.01) and row[4] == "bet" and row[6] == "KXBNB15M-26NOV260915"
    assert orderjournal.pending(s.data_dir) == []
    assert orderjournal.recover_once(s, Client([fill()]), store) == []  # settled: the next pass finds nothing to do
    assert len(bets(store)) == 1


def test_a_no_side_order_is_paid_at_the_no_price(env):
    s, store = env
    sent(s, side="no")
    orderjournal.recover_once(s, Client([fill(n=10, yes=0.60, side="no")]), store)
    [row] = bets(store)
    assert row[1] == "no" and row[3] == pytest.approx(4.0, abs=0.01)  # NO at 1 - 0.60


def test_a_bet_that_has_its_row_is_only_settled(env):
    s, store = env
    sent(s)
    store.add_decision(ticker=TICKER, title="", bid=0.27, ask=0.29, model_p=0.29, edge=0.0, action="bet", mode="trade", close_time="", lane="wave_shape", category="", side="yes", contracts=16.66, stake_usd=4.66)
    orderjournal.recover_once(s, Client([fill()]), store)
    assert len(bets(store)) == 1 and orderjournal.pending(s.data_dir) == []


def test_an_order_still_resting_is_cancelled_before_its_fills_are_counted(env, monkeypatch):
    s, store = env
    monkeypatch.setattr(orderjournal.time, "sleep", lambda _s: None)
    sent(s)
    c = Client([fill(n=5)], status="resting")
    orderjournal.recover_once(s, c, store)
    assert c.cancelled == ["oid-1"]
    assert bets(store)[0][2] == pytest.approx(5)  # what had filled before the cancel


def test_an_order_that_never_filled_leaves_no_bet(env):
    s, store = env
    sent(s)
    out = orderjournal.recover_once(s, Client([]), store)
    assert "nothing filled" in out[0] and bets(store) == [] and orderjournal.pending(s.data_dir) == []


def test_a_failed_pass_leaves_the_order_for_the_next_one(env, caplog):
    s, store = env
    sent(s)
    assert orderjournal.recover_once(s, Client([fill()], fail=1), store) == []  # kalshi is slow: nothing written, nothing lost
    assert "not settled yet" in caplog.text and len(orderjournal.pending(s.data_dir)) == 1
    assert "recovered" in orderjournal.recover_once(s, Client([fill()]), store)[0]


def test_an_order_of_this_process_is_left_to_its_own_evaluation_for_a_while(env):
    s, store = env
    orderjournal.intent(s.data_dir, lane="wave_shape", shot=shot(), order_id="oid-1", count=16.66, price=0.28, kind="resting")
    assert orderjournal.recover_once(s, Client([fill()]), store) == [] and bets(store) == []  # just sent: the buy is still waiting on its fill
    assert len(orderjournal.pending(s.data_dir)) == 1
    assert "recovered" in orderjournal.recover_once(s, Client([fill()]), store, now=time.time() + orderjournal.MIN_AGE + 1)[0]  # long past its evaluation and still no row


def test_an_order_too_old_is_given_up_loudly(env, caplog):
    s, store = env
    sent(s, age=orderjournal.MAX_AGE + 60)
    assert orderjournal.recover_once(s, Client([fill()]), store) == []
    assert "gave up" in caplog.text and orderjournal.pending(s.data_dir) == []


def test_a_journal_that_cannot_be_written_does_not_stop_the_buy(env, monkeypatch, caplog):
    s, _ = env
    monkeypatch.setattr(orderjournal, "_append", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    orderjournal.intent(s.data_dir, lane="wave_shape", shot=shot(), order_id="oid-1", count=1.0, price=0.28, kind="resting")  # no raise
    assert "could not record" in caplog.text


def test_both_buy_paths_record_the_order_before_they_wait():
    import inspect

    from kalshi_moonshot import runner

    src = inspect.getsource(runner)
    assert src.count("orderjournal.intent(") == 2
    resting = inspect.getsource(runner._place_resting)
    assert resting.index("orderjournal.intent(") < resting.index("_rest_wait(")  # before the wait on the fill, not after


def test_a_recovered_bet_gets_its_buy_order_row_so_its_sale_is_a_real_one(env):
    """SOL 00:45 YES, 2026-11-27: the bet was recovered from the journal after a restart, the one rule sold it at 0.84 and the sale was booked on paper, because a live sale sells what the orders table says is held and the recovery wrote no order row; the real contracts went to zero."""
    s, store = env
    sent(s)
    assert store.net_contracts(TICKER, s.env) == 0
    orderjournal.recover_once(s, Client([fill()]), store)
    assert store.net_contracts(TICKER, s.env) == pytest.approx(16.66)  # what a live sale will sell
    ts = store.db.execute("SELECT ts FROM decisions WHERE ticker=? AND action='bet'", (TICKER,)).fetchone()["ts"]
    assert store.entry_fill(TICKER, "yes", float(ts)) == pytest.approx(0.28, abs=0.005)  # and the price the exits judge a profit against
    n = store.db.execute("SELECT COUNT(*) AS n FROM orders WHERE order_id='oid-1'").fetchone()["n"]
    assert n == 1
    orderjournal.recover_once(s, Client([fill()]), store)
    assert store.net_contracts(TICKER, s.env) == pytest.approx(16.66)  # a second pass adds nothing
