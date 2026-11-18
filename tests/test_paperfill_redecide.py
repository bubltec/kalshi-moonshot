"""A paper resting buy that missed is decided again at once, at most buy_redecide times (2026-11-18, user: "shouldn't another tick have gone by with another choice?"; "Yes, paper first")."""
import datetime
import json

import pytest

from kalshi_moonshot import config, paperfill
from kalshi_moonshot.store import Store
from kalshi_moonshot.tape import Tape

T0 = 1_000_000.0
CLOSE = datetime.datetime.fromtimestamp(T0 + 600, datetime.UTC).isoformat().replace("+00:00", "Z")
LANE = """[lane.rest_t]
estimator='ladder'
price=[0.60,0.92]
hours=[0.0,0.26]
timed_suffix='15M'
buy_rest_seconds=5
buy_redecide={n}
"""
NOW = T0 + 5 + paperfill.GRACE + 1


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    def make(n=2):
        (tmp_path / "strategies.toml").write_text(LANE.format(n=n))
        s = config.load()
        return s, Store(s.db_path), Tape(tmp_path / "tape.db")

    return make


def bet(store, ticker="KXGOLD15M-X-15", ts=T0, close=CLOSE, side="yes", lane="rest_t"):
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, title, bid, ask, action, mode, side, lane, contracts, stake_usd, close_time, category, event_ticker) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, ticker, "gold", 0.79, 0.80, "bet", "observe", side, lane, 5, 4.0, close, "Commodities", "KXGOLD15M-X"))
    store.db.commit()


def miss_quotes(tape, ticker="KXGOLD15M-X-15"):
    tape.db.executemany("INSERT INTO quotes (ts, ticker, bid, ask, bid_size, ask_size) VALUES (?,?,?,?,?,?)", [(t, ticker, b, a, 100, 100) for t, b, a in [(T0 - 5, 0.80, 0.81), (T0 + 3, 0.81, 0.83), (T0 + 9, 0.82, 0.84)]])
    tape.db.commit()


def jobs(store):
    return [json.loads(r["payload"]) for r in store.db.execute("SELECT payload FROM jobs WHERE kind='evaluate'").fetchall()]


def test_a_missed_resting_buy_queues_a_fresh_decision_of_the_same_market_for_the_same_lane(world):
    s, store, tape = world()
    bet(store)
    miss_quotes(tape)
    assert paperfill.settle(s, store, tape.db, now=NOW) == {"filled": 0, "missed": 1}
    got = jobs(store)
    assert len(got) == 1 and got[0]["shot"]["ticker"] == "KXGOLD15M-X-15" and got[0]["shot"]["lane"] == "rest_t" and got[0]["shot"]["side"] == "yes"
    from kalshi_moonshot.strategy import LongShot

    assert LongShot(**got[0]["shot"]).ask == 0.80  # the worker builds the shot the same way from the queued payload, then reads the market and replaces the price
    assert got[0]["shot"]["series"] == "KXGOLD15M" and got[0]["shot"]["close_time"] == CLOSE and 0.15 < got[0]["shot"]["hours_to_close"] < 0.17


def test_a_lane_without_the_field_decides_nothing_again(world):
    s, store, tape = world(0)
    bet(store)
    miss_quotes(tape)
    assert paperfill.settle(s, store, tape.db, now=NOW) == {"filled": 0, "missed": 1}
    assert jobs(store) == []


def test_a_bid_that_would_have_filled_is_not_decided_again(world):
    s, store, tape = world()
    bet(store)
    tape.db.executemany("INSERT INTO quotes (ts, ticker, bid, ask, bid_size, ask_size) VALUES (?,?,?,?,?,?)", [(T0 - 5, "KXGOLD15M-X-15", 0.80, 0.81, 100, 100), (T0 + 3, "KXGOLD15M-X-15", 0.78, 0.79, 100, 100)])
    tape.db.commit()
    assert paperfill.settle(s, store, tape.db, now=NOW) == {"filled": 1, "missed": 0}
    assert jobs(store) == []


def test_a_market_is_decided_again_at_most_the_lanes_number_of_times(world):
    s, store, tape = world(2)
    queued = []
    for k in range(4):  # four misses in a row on one market
        ts = T0 + 20 * k
        tape.db.executemany("INSERT INTO quotes (ts, ticker, bid, ask, bid_size, ask_size) VALUES (?,?,?,?,?,?)", [(ts - 5, "KXGOLD15M-X-15", 0.80, 0.81, 100, 100), (ts + 3, "KXGOLD15M-X-15", 0.81, 0.83, 100, 100)])
        tape.db.commit()
        bet(store, ts=ts)
        assert paperfill.settle(s, store, tape.db, now=ts + 11) == {"filled": 0, "missed": 1}
        queued.append(len(jobs(store)))
        store.db.execute("DELETE FROM jobs")  # the worker ran it
        store.db.commit()
    assert queued == [1, 1, 0, 0]  # two fresh decisions, then the market is left alone


def test_a_market_too_close_to_its_close_is_not_decided_again(world):
    s, store, tape = world()
    late_close = datetime.datetime.fromtimestamp(NOW + 20, datetime.UTC).isoformat().replace("+00:00", "Z")  # 20 s left: not room for another rest and the sweep
    bet(store, close=late_close)
    miss_quotes(tape)
    paperfill.settle(s, store, tape.db, now=NOW)
    assert jobs(store) == []
