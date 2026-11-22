"""The resting buy starts under the lower of the ask and its recent average (lane field buy_rest_avg_seconds; 2026-11-22, user, wave-top lane: "when it's peaking it's going to flutter up and down ... you could take an average of like 5s"). The live order (runner._rest_start) and the paper judge (paperfill.settle) read the same rule (restprice.py)."""

import time
from dataclasses import replace

import pytest

from kalshi_moonshot import config, db, paperfill, restprice, runner, ticks
from kalshi_moonshot.lanes import spec_for
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot


def rows(asks, start=95.0):
    return [(start + i, round(a - 0.01, 4), a) for i, a in enumerate(asks)]  # (time, YES bid, YES ask)


def test_the_average_ask_of_each_side_and_the_lower_of_it_and_the_ask():
    r = rows([0.28, 0.28, 0.30, 0.32, 0.34])
    assert restprice.average_ask(r, "yes", 100.0, 5.0) == pytest.approx(0.304)
    assert restprice.average_ask(r, "no", 100.0, 5.0) == pytest.approx(1.0 - 0.294)  # the NO ask is one minus the YES bid
    assert restprice.average_ask(r, "yes", 100.0, 2.0) == pytest.approx(0.33)  # only the quotes at 98 and 99 are within two seconds
    assert restprice.average_ask([], "yes", 100.0, 5.0) is None
    spec = replace(spec_for(config.load(), "commodity"), buy_rest_below=0.02, buy_rest_avg_seconds=5.0)
    assert restprice.rest_price(spec, "yes", 0.34, r, 100.0) == pytest.approx(0.284)  # decided on an up-tick: under the average, not under the ask
    assert restprice.rest_price(spec, "yes", 0.26, r, 100.0) == pytest.approx(0.24)  # decided on a down-tick: under the ask, never raised to the average
    assert restprice.rest_price(spec, "yes", 0.34, [], 100.0) == pytest.approx(0.32)  # nothing in memory: as without the average
    assert restprice.rest_price(replace(spec, buy_rest_avg_seconds=0.0), "yes", 0.34, r, 100.0) == pytest.approx(0.32)  # off


def test_the_live_order_starts_where_the_rule_says_from_the_quote_memory():
    spec = replace(spec_for(config.load(), "commodity"), buy_rest_below=0.02, buy_rest_avg_seconds=5.0)
    now = time.time()
    ticks.mids.clear()
    for i, ask in enumerate([0.28, 0.28, 0.30, 0.32, 0.34]):
        ticks.mids.note("KXBTC15M-LIVE", now - 4 + i, ask - 0.01, ask)
    shot = LongShot(ticker="KXBTC15M-LIVE", title="", rules="", bid=0.33, ask=0.34, volume=0.0, close_time="", hours_to_close=0.2, subtitle="", lane="x", side="yes")
    try:
        assert runner._rest_start(shot, spec) == pytest.approx(0.284, abs=0.004)  # the average of the five quotes is about 0.304
        assert runner._rest_start(shot, replace(spec, buy_rest_avg_seconds=0.0)) == pytest.approx(0.32)
    finally:
        ticks.mids.clear()


def test_the_paper_judge_starts_the_order_under_the_average_too(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    base = "estimator='market'\nprice=[0.18,0.40]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=3.0\nseries=['KXBTC15M']\nbuy_rest_seconds=6\nbuy_rest_below=0.02\n"
    (tmp_path / "strategies.toml").write_text(f"[lane.avg]\n{base}buy_rest_avg_seconds=5\n\n[lane.plain]\n{base}\n")
    monkeypatch.setenv("LIVE_LANES", "none")
    s = config.load()
    store = Store(s.db_path)
    tape = db.connect(s.tape_path, "tape", shared=True)
    tape.execute("CREATE TABLE IF NOT EXISTS quotes (ts REAL NOT NULL, ticker TEXT NOT NULL, bid REAL, ask REAL, last REAL, sts INTEGER, bid_size REAL, ask_size REAL)")
    tape.execute("CREATE TABLE IF NOT EXISTS flow_1s (ts INTEGER NOT NULL, ticker TEXT NOT NULL, yes_vol REAL NOT NULL, no_vol REAL NOT NULL, n INTEGER NOT NULL, vwap REAL)")
    series = {
        "KXBTC15M-UP": ([0.28, 0.28, 0.30, 0.32, 0.34, 0.28, 0.31], 0.34),  # decided on an up-tick at 0.34; the quote after is 0.28
        "KXBTC15M-DOWN": ([0.30, 0.30, 0.30, 0.30, 0.26, 0.23, 0.24], 0.26),  # decided on a down-tick at 0.26
    }
    for tk, (asks, _) in series.items():
        for ts, bid, ask in rows(asks[:5], 96.0) + rows(asks[5:], 102.0):
            tape.execute("INSERT INTO quotes (ts, ticker, bid, ask) VALUES (?, ?, ?, ?)", (ts, tk, bid, ask))
    tape.commit()
    for lane in ("avg", "plain"):
        for tk, (_, ask) in series.items():
            store.add_decision(ticker=tk, title="", bid=0.0, ask=ask, model_p=0.0, edge=0.0, action="bet", mode="observe", lane=lane, side="yes", contracts=3, stake_usd=3.0, close_time="2099-01-01T00:00:00Z", ts=100.0)
    paperfill.settle(s, store, tape, now=1000.0)
    got = {(r["lane"], r["ticker"]): r for r in store.db.execute("SELECT lane, ticker, action, ask FROM decisions").fetchall()}
    assert got[("avg", "KXBTC15M-UP")]["action"] == "bet" and got[("avg", "KXBTC15M-UP")]["ask"] == pytest.approx(0.284)  # under the 0.304 average, not under the 0.34 ask
    assert got[("plain", "KXBTC15M-UP")]["ask"] == pytest.approx(0.32)
    assert got[("avg", "KXBTC15M-DOWN")]["ask"] == pytest.approx(0.24)  # under the ask, never raised to the average
    assert got[("plain", "KXBTC15M-DOWN")]["ask"] == pytest.approx(0.24)
