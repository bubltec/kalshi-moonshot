import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace

from kalshi_moonshot import config, timerecord
from kalshi_moonshot.store import Store


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    store = Store(s.db_path)
    timerecord._cache.clear()
    timerecord._seen.clear()
    return s, store


def _bet(store, i, elapsed, won, lane="mod", now=None):
    now = now or time.time()
    close = now - 1000.0 - i  # settled windows
    ticker = f"KXBTC15M-T{i}"
    store.db.execute("INSERT INTO windows (ticker, series, close_ts, strike, result, fetched_ts) VALUES (?,?,?,?,?,?)", (ticker, "KXBTC15M", close, 100.0, "yes", now))
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, outcome, close_time) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (close - 900.0 + elapsed, ticker, "bet", "trade", 0.4, "yes", lane, 4.0, 1.6, "yes" if won else "no", None),
    )
    store.db.commit()


def test_a_bucket_with_a_negative_record_over_enough_bets_is_sat_out_and_the_rest_stay_open(tmp_path, monkeypatch):
    _, store = _setup(tmp_path, monkeypatch)
    for i in range(10):
        _bet(store, i, 180.0, won=(i % 3 != 0))  # 3 minutes in: mostly wins
    for i in range(10, 20):
        _bet(store, i, 700.0, won=(i % 5 == 0))  # 11-12 minutes in: mostly losses
    for i in range(20, 24):
        _bet(store, i, 420.0, won=False)  # 7 minutes in: only 4 bets, no verdict
    assert timerecord.verdict(store, "mod", 190.0)[0] is False
    skip, bucket, n, score = timerecord.verdict(store, "mod", 710.0)
    assert skip and bucket == 5 and n == 10 and score < 0
    assert timerecord.verdict(store, "mod", 430.0)[0] is False  # under MIN_N bets: open
    assert timerecord.verdict(store, "other_lane", 710.0)[0] is False  # a lane with no record sits out nothing


def test_the_gate_skips_only_with_timing_gate_on_and_the_shadow_logs_once_per_bucket(tmp_path, monkeypatch):
    s, store = _setup(tmp_path, monkeypatch)
    for i in range(10, 20):
        _bet(store, i, 700.0, won=False)
    now = time.time()
    close = now + 900.0 - 705.0  # 705 s into the window
    shot = SimpleNamespace(ticker="KXBTC15M-NEW", lane="mod", side="yes", close_time=datetime.fromtimestamp(close, UTC).isoformat())
    off = SimpleNamespace(timing_gate=False)
    on = SimpleNamespace(timing_gate=True)
    assert timerecord.allows(store, shot, off, now)[0] is True
    ok, why = timerecord.allows(store, shot, on, now)
    assert not ok and "sits this moment out" in why
    timerecord.shadow(s, store, shot, off, 0.35, now)
    timerecord.shadow(s, store, shot, off, 0.36, now + 5)
    rows = [json.loads(x) for x in (tmp_path / "timing_shadow.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["would_skip"] is True and rows[0]["bucket"] == 5


def test_the_shadow_buys_are_scored_against_the_settled_window(tmp_path, monkeypatch):
    s, store = _setup(tmp_path, monkeypatch)
    now = time.time()
    store.db.execute("INSERT INTO windows (ticker, series, close_ts, strike, result, fetched_ts) VALUES (?,?,?,?,?,?)", ("KXBTC15M-S1", "KXBTC15M", now - 600.0, 100.0, "yes", now))
    store.db.commit()
    rows = [
        {"kind": "signal", "ts": now - 1500.0, "ticker": "KXBTC15M-S1", "lane": "mod", "side": "yes", "ask": 0.4, "elapsed": 700.0, "bucket": 5, "n": 10, "score": -0.5, "would_skip": True},
        {"kind": "signal", "ts": now - 1500.0, "ticker": "KXBTC15M-S2", "lane": "mod", "side": "yes", "ask": 0.4, "elapsed": 100.0, "bucket": 0, "n": 3, "score": 0.0, "would_skip": False},
    ]
    (tmp_path / "timing_shadow.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    timerecord._scored_at = 0.0
    new = timerecord.run_score(s, store, now=now)
    assert len(new) == 1 and new[0]["ticker"] == "KXBTC15M-S1" and new[0]["net"] > 1.0 and new[0]["would_skip"] is True  # S2 has not settled
    timerecord._scored_at = 0.0
    assert timerecord.run_score(s, store, now=now + 4000) == []


def _shadow_score(tmp_path, rows):
    with (tmp_path / "timing_shadow.jsonl").open("a") as fh:
        for r in rows:
            fh.write(json.dumps({"kind": "score", "lane": "mod", **r}) + "\n")


def test_old_losses_stop_counting_and_recent_ones_decide(tmp_path, monkeypatch):
    s, store = _setup(tmp_path, monkeypatch)
    now = time.time()
    _shadow_score(tmp_path, [{"ts": now - 2 * 86400 - i, "ticker": f"KXBTC15M-O{i}", "side": "yes", "bucket": 5, "net": -1.0} for i in range(30)])
    skip, _, n, _ = timerecord.verdict(store, "mod", 710.0, now, s.data_dir)
    assert skip is False and n == 0  # thirty losses from two days ago weigh under a bet: the bucket is open again
    timerecord._cache.clear()
    _shadow_score(tmp_path, [{"ts": now - 1800 - i, "ticker": f"KXBTC15M-R{i}", "side": "yes", "bucket": 5, "net": -1.0 if i % 4 else 0.5} for i in range(12)])
    skip, bucket, n, score = timerecord.verdict(store, "mod", 710.0, now, s.data_dir)
    assert skip and bucket == 5 and n >= 4 and score < 0  # recent, enough and clearly negative: sat out


def test_a_skipped_bucket_reopens_when_its_shadow_scores_turn_positive(tmp_path, monkeypatch):
    s, store = _setup(tmp_path, monkeypatch)
    now = time.time()
    _shadow_score(tmp_path, [{"ts": now - 10 * 3600 - i, "ticker": f"KXBTC15M-A{i}", "side": "yes", "bucket": 4, "net": -1.0} for i in range(10)])
    assert timerecord.verdict(store, "mod", 540.0, now, s.data_dir)[0] is False  # ten hours old: 10 x 0.32 = 3.2 of weight, under MIN_W
    timerecord._cache.clear()
    _shadow_score(tmp_path, [{"ts": now - 3600 - i, "ticker": f"KXBTC15M-B{i}", "side": "yes", "bucket": 4, "net": -1.0} for i in range(6)])
    assert timerecord.verdict(store, "mod", 540.0, now, s.data_dir)[0] is True  # six recent losses on top: sat out
    timerecord._cache.clear()
    _shadow_score(tmp_path, [{"ts": now - 600 - i, "ticker": f"KXBTC15M-C{i}", "side": "yes", "bucket": 4, "net": 1.2} for i in range(10)])
    assert timerecord.verdict(store, "mod", 540.0, now, s.data_dir)[0] is False  # ten fresh wins: open again, with no live bet in the bucket


def test_a_candidate_that_was_also_bought_counts_once(tmp_path, monkeypatch):
    s, store = _setup(tmp_path, monkeypatch)
    _bet(store, 1, 700.0, won=False)
    now = time.time()
    close = now - 1000.0 - 1
    _shadow_score(tmp_path, [{"ts": close - 900.0 + 700.0, "ticker": "KXBTC15M-T1", "side": "yes", "bucket": 5, "net": -1.0}])
    w, _, _ = timerecord.record(store, "mod", now, s.data_dir)[5]
    assert 0.9 < w <= 1.0
