"""A resting buy is re-judged on every quote while it waits (restcheck.py, 2026-11-24; user: "Does it keep calculating each tick while it's waiting for the buy? It needs to so it's evaluating logic and updating or removing the buy").

Unit: the verdict per quote (keep at the same price, move lower, move higher, cancel with the step named, a fill that beats the cancel still counts) and its cost (under 60 ms, no table read).
Integration: lane file -> lanes.lane_specs -> runner.evaluate -> resting order -> streamed quotes -> amend in place / cancel, live and paper, and the paper trail judged on the tape by paperfill.settle.
"""

import statistics
import threading
import time

import pytest

from kalshi_moonshot import (
    config,
    db,
    lanes,
    memguard,
    paperfill,
    restbuys,
    restcheck,
    runner,
    steptime,
)
from kalshi_moonshot.kalshi import KalshiError
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot
from tests.test_amend_in_place import Amends
from tests.test_buy_retry import setup  # noqa: F401  (the fixture)
from tests.test_buychain import Fixed
from tests.test_resting_follow import clean, feed, run_place, wait_for  # noqa: F401  (clean: the autouse fixture)

LANE = ("[lane.ch]\nestimator='market'\nprice=[0.10,0.85]\nhours=[0.0,1.0]\nsides=['yes','no']\nfixed_stake=1.0\nseries=['KXBTC15M']\nmax_spread=0.05\nmin_volume=0\n"
        "buy_rest_seconds={secs}\nbuy_rest_below=0.02\nbuy_rest_follow=true\nentry_spike_seconds=90\nbuy_chain={chain}\n")
CLOSE = "2099-01-01T00:00:00Z"


def lane(tmp_path, monkeypatch, chain="['price','spike']", secs=30, live=True):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODE", "trade")
    monkeypatch.setenv("KALSHI_ENV", "demo")
    monkeypatch.setenv("MIN_ORDER_USD", "0.25")
    monkeypatch.setenv("LIVE_LANES", "ch" if live else "none")
    monkeypatch.setenv("COMMODITY_15M_LIVE", "true")
    (tmp_path / "strategies.toml").write_text(LANE.format(chain=chain, secs=secs))
    s = config.load()
    return s, lanes.lane_specs(s)["ch"]


def shot(ask=0.31, bid=0.29, side="yes"):
    return LongShot("KXBTC15M-1", "t", "", bid, ask, 100.0, CLOSE, 0.2, lane="ch", series="KXBTC15M", event_ticker="KXBTC15M-E", side=side)


@pytest.fixture
def calm(monkeypatch):
    """The spike step reads no spike until a test says there is one."""
    state = {"spike": False}
    monkeypatch.setattr(runner, "side_spike", lambda *a, **k: (0.40, 0.10) if state["spike"] else (0.0, 0.01))
    return state


# ---- unit: the verdict ----

def test_unit_the_module_still_buys_at_the_same_price_keeps_the_order(tmp_path, monkeypatch, calm):
    s, spec = lane(tmp_path, monkeypatch)
    v = restcheck.recheck(s, spec, shot(), 0.29, 0.29, 0.31)
    assert v.action == "keep" and v.price == 0.29


def test_unit_a_change_under_one_tick_keeps_the_order_and_its_place_in_the_queue(tmp_path, monkeypatch, calm):
    s, spec = lane(tmp_path, monkeypatch)
    assert restcheck.recheck(s, spec, shot(), 0.2933, 0.29, 0.31).action == "keep"  # wants 0.29, the order is 0.33c off: not a whole tick
    assert not restcheck.differs(0.2933, 0.29) and restcheck.differs(0.30, 0.29)
    assert restcheck.differs(0.0335, 0.035)  # under 10c the grid is a tenth of a cent


def test_unit_a_lower_ask_moves_the_order_down(tmp_path, monkeypatch, calm):
    s, spec = lane(tmp_path, monkeypatch)
    v = restcheck.recheck(s, spec, shot(), 0.29, 0.25, 0.27)
    assert v.action == "move" and v.price == 0.25 and "wants 0.250 (was 0.290)" in v.why


def test_unit_a_higher_ask_moves_the_order_up_while_the_module_still_buys(tmp_path, monkeypatch, calm):
    s, spec = lane(tmp_path, monkeypatch)
    v = restcheck.recheck(s, spec, shot(), 0.29, 0.33, 0.35)
    assert v.action == "move" and v.price == 0.33  # toward the ask, still buy_rest_below under it


def test_unit_a_step_that_now_says_no_cancels_with_its_name_and_words(tmp_path, monkeypatch, calm):
    s, spec = lane(tmp_path, monkeypatch)
    v = restcheck.recheck(s, spec, shot(), 0.29, 0.86, 0.88)
    assert v.action == "cancel" and v.step == "price" and v.why == "out at price (ask 0.88 is outside the lane's 0.1 to 0.85)"
    calm["spike"] = True
    v = restcheck.recheck(s, spec, shot(), 0.29, 0.29, 0.31)
    assert v.action == "cancel" and v.step == "spike" and v.why.startswith("out at spike (price rose +40c")


def test_unit_with_no_module_the_floor_still_withdraws(tmp_path, monkeypatch):
    s, spec = lane(tmp_path, monkeypatch, chain="[]")
    v = restcheck.recheck(s, spec, shot(), 0.29, 0.04, 0.05)
    assert v.action == "cancel" and v.step == "price floor"


def test_unit_the_price_is_on_the_grid_and_under_the_ask():
    from kalshi_moonshot.kalshi import book_price

    assert restcheck.on_grid(0.2633, "yes", 0.30) == 0.26
    assert restcheck.on_grid(0.30, "yes", 0.30) == 0.29  # post-only: never at the ask
    assert restcheck.on_grid(0.0955, "yes", 0.10) == 0.095
    assert restcheck.on_grid(0.894, "no") == 0.89 and book_price(1 - 0.89, "ask") == 0.11  # the GOLD NO buy of 2026-11-23: the NO price goes down, its YES ask (what is sent) is on the grid
    for side in ("yes", "no"):  # what restcheck says the order sits at is exactly what kalshi sends
        for p in (0.0955, 0.2633, 0.5, 0.894, 0.9512):
            g = restcheck.on_grid(p, side)
            sent = book_price(g, "bid") if side == "yes" else 1.0 - book_price(1.0 - g, "ask")
            assert abs(sent - g) < 1e-9 and g <= p + 1e-9


def test_unit_rest_at_bid_lane_prices_on_the_bid_both_ways(tmp_path, monkeypatch, calm):
    s, spec = lane(tmp_path, monkeypatch)
    import dataclasses

    spec = dataclasses.replace(spec, buy_rest_at_bid=True)
    assert restcheck.pricer_for(spec) == "rest_at_bid"
    assert restcheck.recheck(s, spec, shot(), 0.29, 0.29, 0.31).action == "keep"  # on the bid already
    v = restcheck.recheck(s, spec, shot(), 0.29, 0.26, 0.28)
    assert v.action == "move" and v.price == 0.26 and "rest_at_bid" in v.why  # a falling bid
    v = restcheck.recheck(s, spec, shot(), 0.29, 0.32, 0.34)
    assert v.action == "move" and v.price == 0.32  # a rising bid, while the module still buys
    assert restcheck.recheck(s, spec, shot(), 0.29, None, 0.31).action == "keep"  # no bid: kept where it is


def test_unit_the_paper_trail_reads_back():
    mark = restcheck.trail_mark([(100.0, 0.28), (101.5, 0.30)], 100.0, (102.25, "withdrawn at 0.300: out at spike (price rose [+40c])"))
    assert "[" not in mark[1:-1] and runner._TAG.fullmatch(mark)  # one tag, whole
    moves, gone = restcheck.parse_trail(f"x {mark} y", 100.0)
    assert moves == [(100.0, 0.28), (101.5, 0.30)] and gone[0] == pytest.approx(102.25) and gone[1].startswith("withdrawn at 0.300: out at spike")
    assert restcheck.parse_trail("[rest-follow: moves 0.2800@0.00]", 50.0) == ([(50.0, 0.28)], None)
    assert restcheck.parse_trail("no trail", 1.0) is None


def test_unit_one_recheck_is_under_60_ms_and_reads_no_table(tmp_path, monkeypatch, calm):
    from datetime import UTC, datetime, timedelta

    s, spec = lane(tmp_path, monkeypatch, chain="['price','spread','time_left','blackout','volume','min_return','spike']")
    store = Store(s.db_path)
    monkeypatch.setattr(memguard, "raising", True)  # a SELECT or a Kalshi GET inside it raises
    steptime.tick_reset()
    soon = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    sh = LongShot("KXBTC15M-1", "t", "", 0.29, 0.31, 100.0, soon, 10 / 60, lane="ch", series="KXBTC15M", event_ticker="KXBTC15M-E", side="yes")
    judge = restcheck.judge_for(s, spec, sh, p_yes=0.9, store=store, why="fixed")
    times = []
    for i in range(60):
        t0 = time.perf_counter()
        v = judge(0.29, 0.29, 0.31 + (0.01 if i % 2 else 0.0), time.time())
        times.append((time.perf_counter() - t0) * 1000.0)
        assert v.action in ("keep", "move")
    assert statistics.median(times) < 60.0 and max(times) < 60.0
    assert steptime.tick_stats()["buy rest recheck"]["n"] == 60
    assert not memguard.breaches


# ---- unit: the live order thread (runner._rest_follow through runner.place) ----

def test_a_step_that_says_no_while_the_order_waits_cancels_it_and_the_note_names_the_step(setup, calm):
    s, store, sh, spec = setup
    sp = spec(buy_rest_seconds=60.0, buy_retries=0, buy_rest_follow=True, buy_rest_then_take=True, buy_chain=("spike",), entry_spike_seconds=90.0)
    client = Amends(asks=[0.31], account={})
    feed()
    out = {}
    th = threading.Thread(target=lambda: out.update(got=runner.place(s, client, type(store)(store.path), sh, "why", 1.5, order_key="k", judge=restcheck.judge_for(s, sp, sh))), daemon=True, name="placer")
    th.start()
    assert wait_for(lambda: restbuys._by_order.get("r1") is not None)
    calm["spike"] = True
    restbuys.on_event({"kind": "quote", "ticker": sh.ticker, "bid": 0.29, "ask": 0.31, "ts": time.time()})
    th.join(3.0)
    assert not th.is_alive() and client.cancels == ["r1"] and client.amended == [] and len(client.rested) == 1
    got = out["got"]
    assert got.filled == 0 and "withdrawn at 0.300: out at spike (price rose +40c" in got.note
    assert client.sent == []  # a withdrawn order is not taken at the ask afterwards (buy_rest_then_take)


def test_a_fill_that_beats_the_cancel_is_still_counted(setup, calm):
    s, store, sh, spec = setup
    sp = spec(buy_rest_seconds=60.0, buy_retries=0, buy_rest_follow=True, buy_chain=("spike",), entry_spike_seconds=90.0)
    client = Amends(asks=[0.31], account={"r1": 1.5})  # the account's list shows the order filled before the cancel landed
    feed()
    out = {}
    th = threading.Thread(target=lambda: out.update(got=runner.place(s, client, type(store)(store.path), sh, "why", 1.5, order_key="k", judge=restcheck.judge_for(s, sp, sh))), daemon=True, name="placer")
    th.start()
    assert wait_for(lambda: restbuys._by_order.get("r1") is not None)
    calm["spike"] = True
    restbuys.on_event({"kind": "quote", "ticker": sh.ticker, "bid": 0.29, "ask": 0.31, "ts": time.time()})
    th.join(3.0)
    assert client.cancels == ["r1"] and out["got"].filled == 1.5 and out["got"].note.startswith("bought YES 1.5/1.5")


def test_a_move_up_and_a_move_down_amend_the_same_order_for_its_whole_count(setup):
    s, store, sh, spec = setup
    spec(buy_rest_seconds=60.0, buy_retries=0, buy_rest_follow=True)
    client = Exchange(0.31, 0.29, account={"r1": 1.5})
    feed()
    th, out = run_place(s, client, store, sh)
    assert wait_for(lambda: restbuys._by_order.get("r1") is not None)
    client.quote(0.34, 0.36)
    assert wait_for(lambda: len(client.amended) == 1)
    client.quote(0.34, 0.36)  # the same price again: nothing sent
    time.sleep(0.1)
    client.quote(0.25, 0.27)
    assert wait_for(lambda: len(client.amended) == 2)
    assert client.amended == [("r1", "yes", "buy", 0.35, 1.5), ("r1", "yes", "buy", 0.26, 1.5)] and client.cancels == []
    restbuys.on_event({"kind": "fill", "ticker": sh.ticker, "order_id": "r1", "count": 1.5, "action": "buy"})
    th.join(3.0)
    assert out["got"].filled == 1.5


# ---- integration: lane file -> evaluate -> resting order -> quotes -> amend / cancel ----

class Exchange(Amends):
    """A stream-fed exchange whose book is whatever the test last quoted (the side's ask and bid for YES)."""

    def __init__(self, ask, bid, account=None):
        super().__init__(asks=[ask], account=account or {})
        self.ask, self.bid = ask, bid

    def orderbook(self, ticker):
        self.reads += 1
        return {"orderbook_fp": {"no_dollars": [[f"{1.0 - self.ask:.4f}", "100.00"]], "yes_dollars": [[f"{self.bid:.4f}", "100.00"]]}}

    def quote(self, bid, ask):
        self.bid, self.ask = bid, ask
        restbuys.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": bid, "ask": ask, "ts": time.time()})


def run_evaluate(s, client, store):
    out = {}
    th = threading.Thread(target=lambda: out.update(got=runner.evaluate(s, client, None, type(store)(store.path), shot(), estimators={"market": Fixed(0.9)}, order_key="k")), daemon=True, name="placer")
    th.start()
    return th, out


def test_integration_live_lane_file_to_amend_up_and_down_then_a_step_cancels(tmp_path, monkeypatch, calm):
    s, spec = lane(tmp_path, monkeypatch)
    assert spec.buy_rest_follow and spec.buy_chain == ("price", "spike") and s.is_live("ch", "KXBTC15M")
    monkeypatch.setattr(runner.risk, "check", lambda *a, **k: type("V", (), {"ok": True, "reason": ""})())
    monkeypatch.setattr(runner, "_top_up", lambda *a, **k: None)
    store = Store(s.db_path)
    client = Exchange(0.31, 0.29)
    feed()
    th, out = run_evaluate(s, client, store)
    assert wait_for(lambda: restbuys._by_order.get("r1") is not None)
    assert client.rested[0][1] == 0.29  # buy_rest_below (0.02) under the ask
    client.quote(0.25, 0.27)
    assert wait_for(lambda: len(client.amended) == 1)
    client.quote(0.31, 0.33)
    assert wait_for(lambda: len(client.amended) == 2)
    assert [a[3] for a in client.amended] == [0.25, 0.31] and client.cancels == []  # down with the ask, then up with it: amended in place
    calm["spike"] = True
    client.quote(0.31, 0.33)
    th.join(5.0)
    assert not th.is_alive() and client.cancels == ["r1"]
    got = out["got"]
    assert got.action == "pass" and "withdrawn at 0.310: out at spike" in got.note
    row = Store(s.db_path).db.execute("SELECT action, reason FROM decisions WHERE lane='ch'").fetchone()
    assert row["action"] == "pass" and "out at spike" in row["reason"]


def test_integration_paper_lane_is_judged_by_the_same_rechecks(tmp_path, monkeypatch, calm):
    s, _spec = lane(tmp_path, monkeypatch, live=False, secs=20)
    assert not s.is_live("ch", "KXBTC15M")
    store = Store(s.db_path)
    client = Exchange(0.31, 0.29)
    client.cache = type("C", (), {"fed": staticmethod(lambda: True)})()
    feed()
    th, _out = run_evaluate(s, client, store)
    assert wait_for(lambda: restbuys._by_ticker.get("KXBTC15M-1"))
    client.quote(0.31, 0.33)  # the paper order moves up to 0.31
    time.sleep(0.15)
    calm["spike"] = True
    client.quote(0.31, 0.33)  # then the spike step says no: withdrawn
    th.join(5.0)
    assert not th.is_alive() and client.rested == [] and client.amended == [] and client.cancels == []  # paper: nothing sent
    row = Store(s.db_path).db.execute("SELECT id, ts, action, stake_usd, reason FROM decisions WHERE lane='ch'").fetchone()
    assert row["action"] == "bet" and row["stake_usd"] and "[rest-follow: moves 0.2900@0.00 0.3100@" in row["reason"] and "withdrawn at 0.310: out at spike" in row["reason"]
    moves, gone = restcheck.parse_trail(row["reason"], row["ts"])
    # On the tape the ask falls to 0.30 after the withdrawal: the follow rule alone would have filled at 0.31; the order was gone.
    tape = db.connect(s.tape_path, "tape", shared=True)
    tape.execute("CREATE TABLE IF NOT EXISTS quotes (ts REAL NOT NULL, ticker TEXT NOT NULL, bid REAL, ask REAL, last REAL, sts INTEGER, bid_size REAL, ask_size REAL)")
    tape.execute("CREATE TABLE IF NOT EXISTS flow_1s (ts INTEGER NOT NULL, ticker TEXT NOT NULL, yes_vol REAL NOT NULL, no_vol REAL NOT NULL, n INTEGER NOT NULL, vwap REAL)")
    t0 = row["ts"]
    for ts, ask in ((t0 - 1, 0.31), (moves[1][0], 0.33), (gone[0] + 0.5, 0.30)):
        tape.execute("INSERT INTO quotes (ts, ticker, bid, ask) VALUES (?, ?, ?, ?)", (ts, "KXBTC15M-1", round(ask - 0.02, 4), ask))
    tape.commit()
    got = paperfill.settle(s, store, tape, now=t0 + 1000.0)
    judged = store.db.execute("SELECT action, reason FROM decisions WHERE id=?", (row["id"],)).fetchone()
    assert got == {"filled": 0, "missed": 1} and judged["action"] == "pass" and "it was withdrawn at 0.310: out at spike" in judged["reason"]


def test_paper_trail_with_a_move_up_fills_at_the_moved_price(tmp_path, monkeypatch):
    s, _spec = lane(tmp_path, monkeypatch, live=False, secs=6)
    store = Store(s.db_path)
    tape = db.connect(s.tape_path, "tape", shared=True)
    tape.execute("CREATE TABLE IF NOT EXISTS quotes (ts REAL NOT NULL, ticker TEXT NOT NULL, bid REAL, ask REAL, last REAL, sts INTEGER, bid_size REAL, ask_size REAL)")
    tape.execute("CREATE TABLE IF NOT EXISTS flow_1s (ts INTEGER NOT NULL, ticker TEXT NOT NULL, yes_vol REAL NOT NULL, no_vol REAL NOT NULL, n INTEGER NOT NULL, vwap REAL)")
    for ts, ask in ((99.0, 0.31), (101.0, 0.33), (103.0, 0.31)):
        tape.execute("INSERT INTO quotes (ts, ticker, bid, ask) VALUES (?, ?, ?, ?)", (ts, "KXBTC15M-1", round(ask - 0.02, 4), ask))
    tape.commit()
    mark = restcheck.trail_mark([(100.0, 0.29), (101.0, 0.31)], 100.0, None)
    store.add_decision(ticker="KXBTC15M-1", title="", bid=0.29, ask=0.31, model_p=0.9, edge=0.0, action="bet", mode="observe", lane="ch", side="yes", contracts=3, stake_usd=1.0, close_time=CLOSE, ts=100.0, reason=f"chain yes {mark}")
    assert paperfill.settle(s, store, tape, now=1000.0) == {"filled": 1, "missed": 0}
    r = store.db.execute("SELECT action, ask, reason FROM decisions").fetchone()
    assert r["action"] == "bet" and r["ask"] == pytest.approx(0.31) and "followed the ask from 0.29" in r["reason"]  # the ask came back down to the raised order


def test_a_refused_amend_keeps_the_original_deadline(setup):
    s, store, sh, spec = setup
    spec(buy_rest_seconds=60.0, buy_retries=0, buy_rest_follow=True)
    client = Amends(asks=[0.31, 0.36], account={"r2": 1.5}, refuse=True)
    seconds = []
    real = client.buy_resting

    def rest(ticker, side, count, price, secs, client_order_id=None, post_only=True):
        seconds.append(secs)
        return real(ticker, side, count, price, secs, client_order_id, post_only)

    client.buy_resting = rest
    feed()
    th, out = run_place(s, client, store, sh)
    assert wait_for(lambda: restbuys._by_order.get("r1") is not None)
    restbuys.on_event({"kind": "quote", "ticker": sh.ticker, "bid": 0.34, "ask": 0.36, "ts": time.time()})
    assert wait_for(lambda: len(client.rested) == 2)
    assert client.cancels == ["r1"] and client.rested[1][1] == 0.35 and seconds[1] <= seconds[0]  # placed again for what is left of the first order's time, never a fresh minute
    restbuys.on_event({"kind": "fill", "ticker": sh.ticker, "order_id": "r2", "count": 1.5, "action": "buy"})
    th.join(3.0)
    assert out["got"].filled == 1.5


def test_the_resting_wait_still_has_no_timer():
    import inspect

    src = inspect.getsource(runner._rest_wait) + inspect.getsource(runner._paper_rest_wait)
    assert "sleep(" not in src and ".wait(" in src
    assert KalshiError  # (imported for the fakes' refusals)
