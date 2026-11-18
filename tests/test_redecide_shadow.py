"""A live resting buy that missed is decided once more, in shadow (lane field buy_redecide_shadow; 2026-11-18, user: "shouldn't another tick have gone by with another choice?"; "Yes, shadow option"). Integration: lane file -> runner.evaluate (live lane, resting buy) -> the shadow row in data/risk_shadow.jsonl; the dry decision orders nothing and logs no decision."""
import json
import time

import pytest

from kalshi_moonshot import config, regime, riskscore, runner, ticks
from kalshi_moonshot.store import Store
from tests.test_buychain import LANE, RISK_FIRST, Fixed, shot


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.mids.clear()
    ticks.history.clear()


class Book:
    """A client whose book shows an ask of `ask` for YES (a NO bid of one less it) and which never orders."""

    authed = False

    def __init__(self, ask):
        self.ask = ask
        self.orderbooks = 0

    def orderbook(self, ticker):
        self.orderbooks += 1
        return {"orderbook_fp": {"yes_dollars": [[f"{self.ask - 0.01:.4f}", "100.00"]], "no_dollars": [[f"{1.0 - self.ask:.4f}", "100.00"]]}}


def world(tmp_path, monkeypatch, shadow=True, yes_score=0.80):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MODE", "trade")
    monkeypatch.setenv("KALSHI_ENV", "demo")
    monkeypatch.setenv("LIVE_LANES", "ch")
    monkeypatch.setenv("COMMODITY_15M_LIVE", "true")
    (tmp_path / "strategies.toml").write_text(LANE.format(chain=RISK_FIRST) + f"buy_rest_seconds=5\nbuy_rest_then_take=true\nbuy_redecide_shadow={'true' if shadow else 'false'}\n")
    monkeypatch.setattr(riskscore, "live", lambda series, ticker, side, *a, **k: ([yes_score if side == "yes" else 1.0 - yes_score] * 3, 0.5, [0.5] * 3))
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    now = time.time()
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    return s, store


def missing_place(took=True):
    """A place() whose resting bid missed; with `took` the live lane then took the ask (a fill), else it bought nothing."""
    calls = []

    def place(s, client, store, shot, why, count, **k):
        calls.append(count)
        runner._MISS.missed = True
        if took:
            return runner.Placed("bought YES 1/1 at the ask after the resting bid did not fill", 1.0, 0.81, 0.0)
        return runner.Placed("resting buy at 0.79 for 5s did not fill (the ask was 0.80); nothing bought.")

    return place, calls


def rows(tmp_path):
    f = tmp_path / "risk_shadow.jsonl"
    return [x for x in map(json.loads, f.read_text().splitlines()) if x.get("kind") == "redecide"] if f.exists() else []


def decisions(store):
    return store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]


def test_integration_a_missed_live_bid_writes_what_a_fresh_decision_would_have_done_beside_what_the_lane_did(tmp_path, monkeypatch):
    s, store = world(tmp_path, monkeypatch)
    place, calls = missing_place(took=True)
    monkeypatch.setattr(runner, "place", place)
    out = runner.evaluate(s, Book(0.78), None, store, shot(), estimators={"market": Fixed(0.92)})
    assert out.action == "bet" and len(calls) == 1  # the live lane's own buy: one order flow, unchanged
    got = rows(tmp_path)
    assert len(got) == 1
    r = got[0]
    assert r["lane"] == "ch" and r["ticker"] == "KXBTC15M-7" and r["side"] == "yes" and r["live"] is True
    assert r["decided_ask"] == 0.80 and r["ask_now"] == pytest.approx(0.78) and r["rest_price"] == pytest.approx(0.79)
    assert r["live_filled"] == 1.0 and r["live_price"] == pytest.approx(0.81)  # what the live lane did: took the ask
    assert r["would"] == "bet" and r["would_rest"] == pytest.approx(0.77) and "dry" in r["why"]  # a fresh decision at the price now: rest 1c under 0.78
    assert decisions(store) == 1  # the live decision only: the shadow one wrote no row


def test_integration_the_fresh_decision_says_no_when_a_gate_says_no_at_the_price_now(tmp_path, monkeypatch):
    s, store = world(tmp_path, monkeypatch)
    place, _ = missing_place(took=False)
    monkeypatch.setattr(runner, "place", place)
    runner.evaluate(s, Book(0.88), None, store, shot(), estimators={"market": Fixed(0.92)})  # the ask ran to 0.88: over the lane's price band of 0.85
    r = rows(tmp_path)[0]
    assert r["would"] == "pass" and r["would_rest"] is None and r["live_filled"] == 0 and r["live_price"] is None


def test_integration_a_lane_without_the_field_writes_no_shadow_row_and_reads_no_book(tmp_path, monkeypatch):
    s, store = world(tmp_path, monkeypatch, shadow=False)
    place, calls = missing_place()
    monkeypatch.setattr(runner, "place", place)
    book = Book(0.78)
    runner.evaluate(s, book, None, store, shot(), estimators={"market": Fixed(0.92)})
    assert rows(tmp_path) == [] and book.orderbooks == 0 and len(calls) == 1


def test_integration_a_bid_that_filled_writes_no_shadow_row(tmp_path, monkeypatch):
    s, store = world(tmp_path, monkeypatch)

    def filled(s, client, store, shot, why, count, **k):
        runner._MISS.missed = False
        return runner.Placed("bought YES 1/1 resting at 0.79", 1.0, 0.79, 0.79)

    monkeypatch.setattr(runner, "place", filled)
    runner.evaluate(s, Book(0.78), None, store, shot(), estimators={"market": Fixed(0.92)})
    assert rows(tmp_path) == []


def test_integration_a_failing_shadow_never_changes_the_live_buy(tmp_path, monkeypatch, caplog):
    s, store = world(tmp_path, monkeypatch)
    place, calls = missing_place()
    monkeypatch.setattr(runner, "place", place)

    class Broken(Book):
        def orderbook(self, ticker):
            raise RuntimeError("book down")

    out = runner.evaluate(s, Broken(0.78), None, store, shot(), estimators={"market": Fixed(0.92)})
    assert out.action == "bet" and rows(tmp_path) == [] and decisions(store) == 1 and "redecide shadow failed" in caplog.text


def test_unit_a_dry_decision_orders_nothing_and_logs_no_decision_and_says_what_the_real_one_says(tmp_path, monkeypatch):
    s, store = world(tmp_path, monkeypatch)

    def no_order(*a, **k):
        raise AssertionError("a dry decision must not order")

    monkeypatch.setattr(runner, "place", no_order)
    dry = runner.evaluate(s, Book(0.78), None, store, shot(), estimators={"market": Fixed(0.92)}, dry=True)
    assert dry.action == "bet" and decisions(store) == 0
    monkeypatch.setattr(runner, "place", missing_place()[0])
    real = runner.evaluate(s, Book(0.78), None, store, shot(), estimators={"market": Fixed(0.92)})
    assert real.action == dry.action and decisions(store) == 1
