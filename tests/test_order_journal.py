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


# ---- only the buy's own orders count (2026-11-27: with two live lanes on one market the recovery booked the other lane's contracts on an order that never filled) ----
def test_another_orders_fill_on_the_same_market_and_side_is_not_booked_on_an_order_that_never_filled(env):
    """BNB 05:56 on 2026-11-27: wave_shape's resting YES buy of 6.89 contracts did not fill; PriskP then bought 21.42 YES on the same market, and the recovery booked those on the wave lane."""
    s, store = env
    sent(s)
    other = {**fill(n=21.42, yes=0.70, fid="f-priskp"), "order_id": "oid-priskp"}
    out = orderjournal.recover_once(s, Client([other]), store)
    assert "nothing filled" in out[0] and bets(store) == [] and orderjournal.pending(s.data_dir) == []
    assert store.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_the_other_lanes_sale_of_the_other_side_is_not_booked_as_this_buy(env):
    """ZEC 05:45 on 2026-11-27: PriskP sold 25 NO at 0.71, which the exchange lists as a YES fill at 0.29; the wave lane's unfilled YES buy was recovered as 25 YES at about 0.30."""
    s, store = env
    sent(s)
    sale = {**fill(n=25, yes=0.29, fid="f-sale"), "order_id": "oid-priskp-sale"}
    out = orderjournal.recover_once(s, Client([sale]), store)
    assert "nothing filled" in out[0] and bets(store) == []


def test_only_the_buys_own_fill_is_booked_when_both_are_in_the_list(env):
    s, store = env
    sent(s)
    out = orderjournal.recover_once(s, Client([fill(n=5), {**fill(n=21.42, yes=0.70, fid="f2"), "order_id": "oid-priskp"}]), store)
    assert "recovered 5 contracts" in out[0]
    [b] = bets(store)
    assert b[2] == pytest.approx(5) and b[3] == pytest.approx(5 * 0.28)


def test_an_order_the_follow_rule_placed_again_under_a_new_id_is_still_recovered(env):
    s, store = env
    sent(s)
    orderjournal.moved(s.data_dir, "oid-1", "oid-2")
    orderjournal.moved(s.data_dir, "oid-1", "oid-3")
    [e] = orderjournal.pending(s.data_dir)
    assert e["order_ids"] == ["oid-1", "oid-2", "oid-3"]
    out = orderjournal.recover_once(s, Client([{**fill(n=7, fid="f3"), "order_id": "oid-3"}]), store)
    assert "recovered 7 contracts" in out[0] and bets(store)[0][2] == pytest.approx(7)


def test_the_moved_ids_survive_the_journals_compaction(env, monkeypatch):
    s, _ = env
    monkeypatch.setattr(orderjournal, "COMPACT_LINES", 3)
    sent(s)
    orderjournal.moved(s.data_dir, "oid-1", "oid-2")
    for k in range(4):
        orderjournal.settle(s.data_dir, f"gone-{k}", "x")
    assert orderjournal.pending(s.data_dir)[0]["order_ids"] == ["oid-1", "oid-2"]  # this read rewrote the file with only what is pending
    assert orderjournal.pending(s.data_dir)[0]["order_ids"] == ["oid-1", "oid-2"]  # and the ids are still there after it


def test_an_intent_with_no_order_id_books_nothing_and_says_so(env, caplog):
    s, store = env
    orderjournal.intent(s.data_dir, lane="wave_shape", shot=shot(), order_id="", count=16.66, price=0.28, kind="immediate")
    import json

    p = orderjournal.path(s.data_dir)
    row = json.loads(p.read_text().splitlines()[-1])
    row["ts"] = time.time() - 1000.0
    p.write_text(json.dumps(row) + "\n")
    with caplog.at_level("ERROR"):
        out = orderjournal.recover_once(s, Client([fill(n=5)]), store)
    assert "nothing filled" in out[0] and bets(store) == [] and "no order id" in caplog.text


def test_the_resting_wait_tells_the_journal_when_the_order_gets_a_new_id():
    import inspect

    from kalshi_moonshot import runner

    src = inspect.getsource(runner._rest_wait)
    assert "first_id = order_id" in src and "orderjournal.moved(s.data_dir, first_id, order_id)" in src


# ---- 2026-11-28: the entry reasoning travels with the order, and a failed evaluation is booked at once ----
def test_the_journal_keeps_the_entry_reasoning_and_a_recovered_bet_carries_it(env):
    s, store = env
    orderjournal.intent(s.data_dir, lane="wave_shape", shot=shot(), order_id="oid-1", count=16.66, price=0.28, kind="resting", why="wave_low yes (in a dip (half U: bowed 0.29, pace 0.42 of its average)): the fall has run 1.43u")
    e = orderjournal.pending(s.data_dir)[0]
    assert "bowed 0.29" in e["why"]
    out = orderjournal._recover(s, Client(fills=[fill()]), store, e)
    assert "recovered 16.66" in out
    row = store.db.execute("SELECT reason FROM decisions WHERE action='bet' AND ticker=?", (TICKER,)).fetchone()
    assert "RECOVERED" in row["reason"] and "bowed 0.29" in row["reason"] and "was not kept" not in row["reason"]


def test_recover_after_books_an_order_whose_evaluation_ended_without_a_bet_and_leaves_other_markets_alone(env):
    s, store = env
    since = time.time() - 2.0
    orderjournal.intent(s.data_dir, lane="wave_shape", shot=shot(), order_id="oid-1", count=16.66, price=0.28, kind="resting", why="the reasoning")
    other = shot()
    other = other._replace(ticker="KXSOL15M-26NOV260915-15") if hasattr(other, "_replace") else other
    got = orderjournal.recover_after(s, Client(fills=[fill()]), store, "KXOTHER15M-X", "yes", since)
    assert got == []  # another market: untouched
    assert orderjournal.pending(s.data_dir), "the intent is still pending"
    got = orderjournal.recover_after(s, Client(fills=[fill()]), store, TICKER, "yes", since)  # no MIN_AGE wait: the evaluation has ended
    assert got and "recovered 16.66" in got[0]
    assert not orderjournal.pending(s.data_dir)
    assert store.db.execute("SELECT COUNT(*) AS n FROM decisions WHERE action='bet' AND ticker=?", (TICKER,)).fetchone()["n"] == 1
    assert store.db.execute("SELECT COUNT(*) AS n FROM orders WHERE order_id='oid-1'").fetchone()["n"] == 1  # the order row too: a live sale sells what the orders table says the account holds


def test_integration_an_evaluation_that_raises_after_the_order_is_booked_from_the_journal_at_once(env, monkeypatch):
    """The socket path (Worker.evaluate_shot): the order was sent (the journal has it), then the evaluation raised and no bet row was made. The `finally` books it from the journal now, with the reasoning, and does it for that market and side only; a purchase that returned 'bought' is left to its own row."""
    from kalshi_moonshot import orderkeys
    from kalshi_moonshot.worker import Worker

    s, store = env
    monkeypatch.setenv("LIVE_LANES", "wave_shape")
    s = config.load()
    store = Store(s.db_path)

    class Cache:
        def fed(self):
            return True

    class C(Client):
        authed = True
        cache = Cache()

    client = C(fills=[fill()])
    w = Worker(s, client, None, store, None)
    w._mem_ready.set()
    monkeypatch.setattr(orderkeys, "ready", lambda: True)
    monkeypatch.setattr(w, "_venue", lambda lane: client)

    def boom(self, shot, key, **kw):
        orderjournal.intent(s.data_dir, lane="wave_shape", shot=shot, order_id="oid-1", count=16.66, price=0.28, kind="resting", why="wave_low yes: the reasoning the order was sent on")
        raise RuntimeError("the evaluation broke after the order")

    monkeypatch.setattr(Worker, "_evaluate_shot", boom)
    monkeypatch.setattr(s.__class__, "is_live", lambda self, lane, series=None: True, raising=False)
    sh = shot()._replace(lane="wave_shape") if hasattr(shot(), "_replace") else shot()
    with pytest.raises(RuntimeError):
        w.evaluate_shot(sh)
    row = store.db.execute("SELECT reason, lane FROM decisions WHERE action='bet' AND ticker=?", (TICKER,)).fetchone()
    assert row is not None and "RECOVERED" in row["reason"] and "the reasoning the order was sent on" in row["reason"]
    assert not orderjournal.pending(s.data_dir)

    def bought(self, shot, key, **kw):
        orderjournal.intent(s.data_dir, lane="wave_shape", shot=shot, order_id="oid-2", count=5.0, price=0.30, kind="resting", why="x")
        return "bought YES 5/5 resting at 0.30"

    monkeypatch.setattr(Worker, "_evaluate_shot", bought)
    w.evaluate_shot(sh)
    assert [e["order_id"] for e in orderjournal.pending(s.data_dir)] == ["oid-2"], "a purchase's own row is not duplicated by the recovery"
