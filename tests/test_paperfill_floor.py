"""A paper buy that a live lane would have rested at its price floor is judged on the tape, like a resting buy (paperfill.py)."""
import pytest

from kalshi_moonshot import config, paperfill
from kalshi_moonshot.store import Store
from kalshi_moonshot.tape import Tape

T0 = 1_000_000.0
LANE = """[lane.floor_t]
estimator='ladder'
price=[0.78,0.92]
hours=[0.0,0.26]
timed_suffix='15M'
entry_approach=0.10
entry_arm_seconds=30
"""
MARKED = "[floor-bid 30s: a paper bid at the floor 0.78, judged on the tape] fixed stake"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE)
    s = config.load()
    return s, Store(s.db_path), Tape(tmp_path / "tape.db")


def bet(store, ticker, reason=MARKED, ts=T0):
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ts, ticker, "bet", "observe", 0.78, "yes", "floor_t", 5, 3.9, reason))
    store.db.commit()


def quotes(tape, ticker, rows):
    tape.db.executemany("INSERT INTO quotes (ts, ticker, bid, ask, bid_size, ask_size) VALUES (?,?,?,?,?,?)", [(t, ticker, b, a, 100, 100) for t, b, a in rows])
    tape.db.commit()


def row(store, ticker):
    return store.db.execute("SELECT action, reason FROM decisions WHERE ticker=?", (ticker,)).fetchone()


NOW = T0 + 30 + paperfill.GRACE + 1


def test_a_floor_bid_the_ask_came_down_to_is_a_filled_bet(world):
    s, store, tape = world
    bet(store, "KXBTC15M-A")
    quotes(tape, "KXBTC15M-A", [(T0 - 5, 0.79, 0.80), (T0 + 10, 0.76, 0.78), (T0 + 20, 0.80, 0.82)])
    assert paperfill.settle(s, store, tape.db, now=NOW) == {"filled": 1, "missed": 0}
    assert row(store, "KXBTC15M-A")["action"] == "bet"


def test_a_floor_bid_the_ask_never_came_back_to_is_a_pass(world):
    s, store, tape = world
    bet(store, "KXBTC15M-B")
    quotes(tape, "KXBTC15M-B", [(T0 - 5, 0.83, 0.85), (T0 + 10, 0.84, 0.86), (T0 + 25, 0.85, 0.87)])
    assert paperfill.settle(s, store, tape.db, now=NOW) == {"filled": 0, "missed": 1}
    got = row(store, "KXBTC15M-B")
    assert got["action"] == "pass" and "would not have filled" in got["reason"]


def test_a_floor_bid_sent_when_the_ask_was_already_at_it_would_have_been_refused(world):
    s, store, tape = world
    bet(store, "KXBTC15M-C")
    quotes(tape, "KXBTC15M-C", [(T0 - 1, 0.77, 0.78), (T0 + 10, 0.76, 0.78)])
    assert paperfill.settle(s, store, tape.db, now=NOW) == {"filled": 0, "missed": 1}  # post only: it would have crossed the book


def test_a_taker_buy_on_the_same_lane_is_left_as_it_was_logged(world):
    s, store, tape = world
    bet(store, "KXBTC15M-D", reason="bought at the ask. fixed stake")
    quotes(tape, "KXBTC15M-D", [(T0 - 5, 0.83, 0.85), (T0 + 25, 0.85, 0.87)])
    assert paperfill.settle(s, store, tape.db, now=NOW) == {"filled": 0, "missed": 0}
    assert row(store, "KXBTC15M-D")["action"] == "bet"


def test_a_floor_bid_still_inside_its_window_is_not_judged_yet(world):
    s, store, tape = world
    bet(store, "KXBTC15M-E")
    quotes(tape, "KXBTC15M-E", [(T0 - 5, 0.83, 0.85)])
    assert paperfill.settle(s, store, tape.db, now=T0 + 20) == {"filled": 0, "missed": 0}
    assert row(store, "KXBTC15M-E")["action"] == "bet"


def test_a_floor_bid_is_judged_once(world):
    s, store, tape = world
    bet(store, "KXBTC15M-F")
    quotes(tape, "KXBTC15M-F", [(T0 - 5, 0.79, 0.80), (T0 + 10, 0.76, 0.78)])
    assert paperfill.settle(s, store, tape.db, now=NOW)["filled"] == 1
    assert paperfill.settle(s, store, tape.db, now=NOW + 60) == {"filled": 0, "missed": 0}


def test_evaluate_marks_a_paper_floor_bid_and_leaves_a_normal_paper_buy_alone(tmp_path):
    from datetime import UTC, datetime, timedelta

    from kalshi_moonshot import runner
    from kalshi_moonshot.strategy import LongShot

    class Est:
        def estimate(self, *a, **k):
            return 0.30, "because"

    class Client:
        authed = True

        def balance(self):
            return {"balance": 20000, "balance_dollars": "200.0000", "portfolio_value": 0}

    base = config.load()
    s = type(base)(**{**base.__dict__, "data_dir": tmp_path, "mode": "trade", "env": "demo", "min_order_usd": 0.0, "paper_bankroll_usd": 100.0,
                      "skip_categories": frozenset(), "commodities_enabled": True, "max_usd_per_trade": 5.0, "max_loss_per_day": 100.0,
                      "max_open_positions": 20, "live_lanes": frozenset({"steady"})})
    store = Store(s.db_path)
    close = (datetime.now(UTC) + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def shot(ticker):
        return LongShot(ticker, "t", "", 0.09, 0.10, 500.0, close, 10.0, lane="moonshot", event_ticker=ticker)

    runner.evaluate(s, Client(), Est(), store, shot("KXM-F"), rest_seconds=30.0)
    runner.evaluate(s, Client(), Est(), store, shot("KXM-N"))
    floor = store.db.execute("SELECT action, reason FROM decisions WHERE ticker='KXM-F'").fetchone()
    plain = store.db.execute("SELECT action, reason FROM decisions WHERE ticker='KXM-N'").fetchone()
    assert floor["action"] == "bet" and floor["reason"].startswith(paperfill.FLOOR_MARK + " 30s")
    assert plain["action"] == "bet" and paperfill.FLOOR_MARK not in plain["reason"]
