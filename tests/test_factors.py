import json

import pytest

from kalshi_moonshot import config, factors
from kalshi_moonshot.store import Store
from kalshi_moonshot.tape import Tape

NOW = 10_000.0
CLOSE = NOW + 600.0  # ten minutes left: five minutes into the window


@pytest.fixture
def tape(tmp_path):
    t = Tape(tmp_path / "tape.db")
    quotes = [  # (ts, yes bid, yes ask, bid size, ask size)
        (NOW - 100, 0.30, 0.34, 100, 300), (NOW - 40, 0.32, 0.36, 100, 300), (NOW - 5, 0.35, 0.39, 400, 100),
    ]
    t.db.executemany("INSERT INTO quotes (ts, ticker, bid, ask, bid_size, ask_size) VALUES (?,?,?,?,?,?)", [(q[0], "KXBTC15M-X", q[1], q[2], q[3], q[4]) for q in quotes])
    t.db.executemany("INSERT INTO flow_1s VALUES (?,?,?,?,?,?)", [
        (int(NOW - 50), "KXBTC15M-X", 30.0, 10.0, 4, 0.5), (int(NOW - 10), "KXBTC15M-X", 20.0, 0.0, 2, 0.5),
    ])
    t.db.executemany("INSERT INTO index_ticks VALUES (?,?,?,?,?,?)", [  # the index 20 minutes before the snapshot, and as the window opened (5 minutes before it)
        (NOW - 1200, "BRTI", 100000.0, None, None, None), (NOW - 300, "BRTI", 100100.0, None, None, None)])
    t.db.commit()
    return t.db


def test_the_yes_side_snapshot(tape):
    f = factors.snapshot(tape, "KXBTC15M-X", "yes", CLOSE, NOW)
    assert f["elapsed_min"] == pytest.approx(5.0) and f["spread"] == pytest.approx(0.04)
    assert f["imbalance"] == pytest.approx(0.6)  # 400 bidding against 100 offered
    assert f["move_90"] == pytest.approx(0.05)  # the bid was 0.30 at the last quote 90 s or more ago, 0.35 now
    assert f["range_90"] == pytest.approx(0.03)  # inside the last 90 s the bid went 0.32 to 0.35
    assert f["activity_60"] == pytest.approx(60.0)
    assert f["prior_15"] == pytest.approx(10.0)  # the index rose 0.1% (10 bp) over the 15 minutes before the window opened
    assert set(f) == set(factors.FEATURES)  # the report reads exactly these; snapshot_full adds the rest


def test_the_no_side_sees_the_book_the_other_way_round(tape):
    f = factors.snapshot(tape, "KXBTC15M-X", "no", CLOSE, NOW)
    assert f["spread"] == pytest.approx(0.04) and f["imbalance"] == pytest.approx(-0.6)
    assert f["move_90"] == pytest.approx(-0.05)
    assert f["prior_15"] == pytest.approx(-10.0)  # the same rise is a fall for a NO bet


def test_no_recent_quote_means_no_snapshot(tape):
    assert factors.snapshot(tape, "KXBTC15M-X", "yes", CLOSE, NOW + 500) == {}
    assert factors.snapshot(tape, "OTHER", "yes", CLOSE, NOW) == {}
    assert factors.snapshot(object(), "KXBTC15M-X", "yes", CLOSE, NOW) == {}  # a broken tape never raises


def test_the_json_form_needs_a_close_time(tape):
    assert factors.snapshot_json(tape, "KXBTC15M-X", "yes", "not a time", NOW) == ""
    assert json.loads(factors.snapshot_json(tape, "KXBTC15M-X", "yes", "1970-01-01T02:56:40Z", NOW))["spread"] == pytest.approx(0.04)


def test_return_per_dollar_after_fees():
    held_win = factors.bet_roi(0.40, "yes", "yes", None)
    assert held_win == pytest.approx((1 - 0.4168) / 0.4168, abs=1e-3)  # no fee at settlement
    assert factors.bet_roi(0.40, "yes", "no", None) == -1.0
    assert factors.bet_roi(0.40, "no", "no", None) == pytest.approx(held_win)
    sold = factors.bet_roi(0.40, "yes", None, 0.60)  # the sale pays its own fee
    assert sold == pytest.approx((0.60 - 0.0168) / 0.4168 - 1, abs=1e-3)
    assert factors.bet_roi(0.40, "yes", None, None) is None  # still open


def test_decisions_keep_their_snapshot_and_the_report_reads_it(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    store = Store(s.db_path)
    assert "factors" in {r["name"] for r in store.db.execute("PRAGMA table_info(decisions)")}
    short = factors.report(store, None, 30)
    assert "too few" in short[0]
    for i in range(60):  # spread decides the result: wide spreads lose
        wide = i % 2 == 0
        snap = {"spread": 0.05 if wide else 0.01, "imbalance": 0.0, "elapsed_min": 5.0}
        store.add_decision(ticker=f"T{i}", title="t", bid=0.3, ask=0.4, action="bet", mode="observe", lane="x", side="yes",
                           outcome="no" if wide else "yes", factors=json.dumps(snap))
    out = "\n".join(factors.report(store, "x", 30))
    assert "60 finished bets" in out and "spread" in out
    low = next(line for line in out.splitlines() if line.strip().startswith("low") and "bets" in line)
    assert "+" in low  # the low-spread third made money (the wide third lost everything)


def test_the_verdict_calls_noise_noise_and_a_clear_gap_a_signal():
    assert "noise" in factors.verdict({"low": (-0.05, 0.03), "high": (-0.03, 0.03)})
    assert "candidate" in factors.verdict({"low": (-0.12, 0.02), "high": (-0.06, 0.02)})
    assert "signal" in factors.verdict({"low": (-0.15, 0.02), "high": (0.0, 0.02)})
    assert "too little" in factors.verdict({"low": (-0.1, float("nan"))})


def test_the_prior_move_is_empty_where_there_is_no_index(tape):
    tape.execute("UPDATE quotes SET ticker='KXGOLD15M-X' WHERE ticker='KXBTC15M-X'")
    assert factors.snapshot(tape, "KXGOLD15M-X", "yes", CLOSE, NOW)["prior_15"] is None  # a commodity has no settlement index on the tape


def test_the_prior_move_is_empty_when_the_index_ticks_are_missing(tape):
    tape.execute("DELETE FROM index_ticks")
    assert factors.snapshot(tape, "KXBTC15M-X", "yes", CLOSE, NOW)["prior_15"] is None


def test_the_full_snapshot_adds_the_price_moves_and_the_book(tape):
    f = factors.snapshot_full(tape, "KXBTC15M-X", "yes", CLOSE, NOW)
    assert set(factors.FEATURES) <= set(f)  # everything the report reads is still there
    assert f["bid"] == pytest.approx(0.35) and f["ask"] == pytest.approx(0.39)
    assert f["rise_30"] == pytest.approx(0.03) and f["rise_90"] == pytest.approx(0.05)  # the ask 30 s ago was 0.36, 90 s ago 0.34
    assert f["updates_60"] == 2 and f["held_min"] is None  # the ask never reached 70c
    g = factors.snapshot_full(tape, "KXBTC15M-X", "no", CLOSE, NOW)
    assert g["bid"] == pytest.approx(0.61) and g["ask"] == pytest.approx(0.65)


def test_a_market_that_is_not_a_15_minute_window_has_no_window_features(tape):
    tape.execute("INSERT INTO quotes (ts, ticker, bid, ask, bid_size, ask_size) VALUES (?,?,?,?,?,?)", (NOW - 2, "KXBTCD-X", 0.5, 0.52, 10, 10))
    tape.commit()
    f = factors.snapshot_full(tape, "KXBTCD-X", "yes", CLOSE, NOW)
    assert f["elapsed_min"] is None and f["prior_15"] is None and f["spread"] == pytest.approx(0.02)


def test_an_empty_snapshot_stays_empty(tape):
    assert factors.snapshot_full(tape, "OTHER", "yes", CLOSE, NOW) == {}
