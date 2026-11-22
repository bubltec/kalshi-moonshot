"""A paper resting order is a bet only while its time lasts (2026-11-22, user, after the wave lane's 12:16 NEAR and HYPE: a second look 5 s after a resting buy found a lower entry and was refused as "already holding", then recorded a fill with no stake). The live lane's order is over when its wait is, and its next look places a new one; the paper bet is judged on the tape a few seconds later, so in between it must not count as a hold, and the judge must not buy twice when the first one filled."""

import pytest

from kalshi_moonshot import config, db, paperfill
from kalshi_moonshot.store import Store, book_holds_sized


def row(ts, reason="", stake=7.5, lane="wave"):
    return {"ticker": "KXBTC15M-A", "lane": lane, "stake_usd": stake, "outcome": None, "exit_ts": None, "ts": ts, "reason": reason}


def test_an_expired_unjudged_resting_order_is_no_hold_but_a_live_one_and_a_judged_fill_are():
    assert book_holds_sized([row(100.0)], "KXBTC15M-A", "wave")  # no limit given: any open sized bet holds
    assert book_holds_sized([row(100.0)], "KXBTC15M-A", "wave", pending_before=99.0)  # the order's time is not up yet
    assert not book_holds_sized([row(100.0)], "KXBTC15M-A", "wave", pending_before=105.0)  # its time is up and nobody has judged it
    assert book_holds_sized([row(100.0, "[rest-v2: a resting bid at 0.25 would have filled]")], "KXBTC15M-A", "wave", pending_before=105.0)  # judged: a fill is a position
    assert not book_holds_sized([row(100.0, stake=None)], "KXBTC15M-A", "wave", pending_before=105.0)  # a re-evaluation row with no stake is nothing


def test_the_judge_skips_stakeless_rows_and_will_not_buy_a_market_twice(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(
        "[lane.rests]\nestimator='market'\nprice=[0.18,0.40]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=3.0\nseries=['KXBTC15M']\nbuy_rest_seconds=5\nbuy_rest_below=0.02\n")
    monkeypatch.setenv("LIVE_LANES", "none")
    s = config.load()
    store = Store(s.db_path)
    tape = db.connect(s.tape_path, "tape", shared=True)
    tape.execute("CREATE TABLE IF NOT EXISTS quotes (ts REAL NOT NULL, ticker TEXT NOT NULL, bid REAL, ask REAL, last REAL, sts INTEGER, bid_size REAL, ask_size REAL)")
    tape.execute("CREATE TABLE IF NOT EXISTS flow_1s (ts INTEGER NOT NULL, ticker TEXT NOT NULL, yes_vol REAL NOT NULL, no_vol REAL NOT NULL, n INTEGER NOT NULL, vwap REAL)")
    for tk, asks in {  # YES asks every two seconds from 98: the first order (0.28 at ts 100) fills at 102 on TWICE
        "KXBTC15M-TWICE": [0.30, 0.30, 0.30, 0.26, 0.26, 0.26, 0.26, 0.26, 0.26, 0.26, 0.26],
        "KXBTC15M-LATE": [0.30, 0.30, 0.30, 0.30, 0.30, 0.30, 0.26, 0.26, 0.26, 0.26, 0.26],  # the first order (ts 100, to 105) never fills; the second (ts 106, rests 0.28, to 111) fills at 110
    }.items():
        for i, ask in enumerate(asks):
            tape.execute("INSERT INTO quotes (ts, ticker, bid, ask) VALUES (?, ?, ?, ?)", (98.0 + 2 * i, tk, round(ask - 0.01, 4), ask))
    tape.commit()

    def decide(tk, ts, stake=3.0):
        return store.add_decision(ticker=tk, title="", bid=0.0, ask=0.30, model_p=0.0, edge=0.0, action="bet", mode="observe", lane="rests", side="yes", contracts=3 if stake else None, stake_usd=stake, close_time="2099-01-01T00:00:00Z", ts=ts)

    ids = {"t1": decide("KXBTC15M-TWICE", 100.0), "t_stakeless": decide("KXBTC15M-TWICE", 103.0, stake=None), "t2": decide("KXBTC15M-TWICE", 106.0),
           "l1": decide("KXBTC15M-LATE", 100.0), "l2": decide("KXBTC15M-LATE", 106.0)}
    got = paperfill.settle(s, store, tape, now=1000.0)
    by = {k: store.db.execute("SELECT action, ask, stake_usd, reason FROM decisions WHERE id=?", (i,)).fetchone() for k, i in ids.items()}
    assert by["t1"]["action"] == "bet" and by["t1"]["ask"] == pytest.approx(0.28)
    assert by["t_stakeless"]["action"] == "bet" and by["t_stakeless"]["stake_usd"] is None and "rest-v2" not in (by["t_stakeless"]["reason"] or "")  # not a bet: never judged, never given a fill
    assert by["t2"]["action"] == "pass" and "already holding this market (paper)" in by["t2"]["reason"]  # the first order filled and is open
    assert by["l1"]["action"] == "pass"  # its order never filled
    assert by["l2"]["action"] == "bet" and by["l2"]["ask"] == pytest.approx(0.28)  # so the second look's order stands
    assert got == {"filled": 2, "missed": 2}
