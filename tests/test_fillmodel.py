import dataclasses

import pytest

from kalshi_moonshot import config, db, fillmodel, misses, paperfill
from kalshi_moonshot.store import Store


def test_a_taker_buy_fills_at_the_asks_of_its_own_side_inside_the_limit():
    q = [(100.0, 0.80, 0.82, 50, 50), (101.0, 0.80, 0.86, 50, 50), (102.0, 0.84, 0.86, 50, 50)]
    assert fillmodel.simulate_taker_buy(q, "yes", 100.0, 0.82, 0.03) == fillmodel.Fill(True, 0.82)
    assert fillmodel.simulate_taker_buy(q, "yes", 100.5, 0.82, 0.03) == fillmodel.Fill(False, None)  # the next quote is 0.86, over the 0.85 limit
    assert fillmodel.simulate_taker_buy(q, "yes", 100.5, 0.82, 0.03, window=1) == fillmodel.Fill(False, None)
    no = fillmodel.simulate_taker_buy(q, "no", 102.0, 0.17, 0.03)  # the NO ask is 1 - the YES bid
    assert no == fillmodel.Fill(True, 0.16)
    assert fillmodel.simulate_taker_buy(q, "yes", 200.0, 0.82, 0.03) == fillmodel.Fill(False, None)  # no quote after the decision


def test_a_depth_rule_refuses_a_thin_book():
    thin = [(100.0, 0.80, 0.82, 5, 5)]
    assert fillmodel.simulate_taker_buy(thin, "yes", 100.0, 0.82, 0.03, stake_usd=6.0, depth_ratio=0.5).filled is True  # half of $6 is $3 and the book shows 5 contracts at 0.82 = $4.10
    assert fillmodel.simulate_taker_buy(thin, "yes", 100.0, 0.82, 0.03, stake_usd=10.0, depth_ratio=0.5).filled is False  # half of $10 is $5: the book is too thin
    assert fillmodel.simulate_taker_buy(thin, "yes", 100.0, 0.82, 0.03, stake_usd=6.0, depth_ratio=0.0).filled is True


def test_misses_are_told_apart_from_fills_and_from_plain_paper_bets():
    assert misses.classify("trade", "bought") == "filled"
    assert misses.classify("observe", "spike ... (live order not filled: blocked by risk)") == "missed"
    assert misses.classify("observe", "a paper bet") is None
    rows = [
        {"lane": "L", "mode": "trade", "reason": "x", "side": "yes", "ask": 0.10, "stake_usd": 1.0, "result": "no"},
        {"lane": "L", "mode": "trade", "reason": "x", "side": "yes", "ask": 0.10, "stake_usd": 1.0, "result": "yes"},
        {"lane": "L", "mode": "observe", "reason": "live order not filled", "side": "no", "ask": 0.20, "stake_usd": 1.0, "result": "no"},
        {"lane": "L", "mode": "observe", "reason": "paper", "side": "no", "ask": 0.20, "stake_usd": 1.0, "result": "no"},
        {"lane": "L", "mode": "trade", "reason": "x", "side": "yes", "ask": 0.10, "stake_usd": 1.0, "result": None},
    ]
    a = misses.summarize(rows)["L"]
    assert (a["filled"], a["filled_won"], a["missed"], a["missed_won"]) == (2, 1, 1, 1)
    assert a["filled_payout"] - a["filled_staked"] == pytest.approx(8.0) and a["missed_payout"] - a["missed_staked"] == pytest.approx(4.0)


def test_a_taker_paper_bet_is_refilled_or_turned_into_a_pass_on_the_tape(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.paperlane]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=3.0\nseries=['KXBTC15M']\nbuy_slip=0.03\npaper_fill_model=true\n")
    monkeypatch.setenv("LIVE_LANES", "none")
    s = config.load()
    assert dataclasses.replace(paperfill.lane_specs(s)["paperlane"]).paper_fill_model
    store = Store(s.db_path)
    tape = db.connect(s.tape_path, "tape", shared=True)
    tape.execute("CREATE TABLE IF NOT EXISTS quotes (ts REAL NOT NULL, ticker TEXT NOT NULL, bid REAL, ask REAL, last REAL, sts INTEGER, bid_size REAL, ask_size REAL)")
    tape.execute("INSERT INTO quotes (ts, ticker, bid, ask, bid_size, ask_size) VALUES (100.5, 'KXBTC15M-A', 0.81, 0.83, 10, 10)")  # within the 0.80 + 0.03 limit
    tape.execute("INSERT INTO quotes (ts, ticker, bid, ask, bid_size, ask_size) VALUES (200.5, 'KXBTC15M-B', 0.85, 0.88, 10, 10)")  # over it
    tape.commit()
    for tk, ts in (("KXBTC15M-A", 100.0), ("KXBTC15M-B", 200.0)):
        store.add_decision(ticker=tk, title="", bid=0.0, ask=0.80, model_p=0.0, edge=0.0, action="bet", mode="observe", lane="paperlane", side="yes", contracts=3, stake_usd=3.0, close_time="2099-01-01T00:00:00Z", ts=ts)
    got = paperfill.settle_taker(s, store, tape, now=1000.0)
    assert got == {"filled": 1, "missed": 1}
    a = store.db.execute("SELECT action, ask, reason FROM decisions WHERE ticker='KXBTC15M-A'").fetchone()
    b = store.db.execute("SELECT action, reason FROM decisions WHERE ticker='KXBTC15M-B'").fetchone()
    assert a["action"] == "bet" and a["ask"] == pytest.approx(0.83) and "filled at 0.830" in a["reason"]
    assert b["action"] == "pass" and "not filled" in b["reason"]
    assert paperfill.settle_taker(s, store, tape, now=1000.0) == {"filled": 0, "missed": 0}  # each bet is judged once
