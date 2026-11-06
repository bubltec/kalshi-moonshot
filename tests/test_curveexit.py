import json
import math
from types import SimpleNamespace

from kalshi_moonshot import curveexit


def series(values, start=1000.0):
    return [start + i for i in range(len(values))], list(values)


def wiggle(n, amp=1e-4, base=100.0):
    """A choppy 1 s series: it steps up and down around base."""
    return [base * math.exp(amp * (1 if i % 2 else -1)) for i in range(n)]


def test_a_spot_that_fell_off_its_high_has_bounced_for_a_yes_bet():
    vals = wiggle(900) + [100.0 * math.exp(-0.002 * k) for k in range(1, 6)]  # a drop at the end, against YES
    ts, vs = series(vals)
    r = curveexit.readings(ts, vs, "yes", ts[-1])
    assert r and r["bounce"] > 3.0
    assert curveexit.readings(ts, vs, "no", ts[-1])["bounce"] < 1.0  # the same fall is with a NO bet


def test_a_short_or_stale_history_has_no_reading():
    ts, vs = series(wiggle(40))
    assert curveexit.readings(ts, vs, "yes", ts[-1]) is None
    ts, vs = series(wiggle(900))
    assert curveexit.readings(ts, vs, "yes", ts[-1] + 60.0) is None


def test_a_straight_run_is_efficient_and_a_wiggle_is_not():
    ts, vs = series(wiggle(900))
    assert curveexit.readings(ts, vs, "yes", ts[-1])["eff"] < 0.05
    run = [100.0 * math.exp(1e-4 * i) for i in range(900)]
    ts, vs = series(run)
    assert curveexit.readings(ts, vs, "yes", ts[-1])["eff"] > 0.95


def cuts():
    return {"p01": 0.01, "p33": 0.07, "p67": 0.12, "p99": 0.9, "unit_med": 0.001, "n": 100.0}


def test_state_reads_the_markets_own_cutoffs_and_sits_out_anomalies():
    c = cuts()
    assert curveexit.state({"eff": 0.03, "unit": 0.001}, c) == "choppy"
    assert curveexit.state({"eff": 0.09, "unit": 0.001}, c) == "middle"
    assert curveexit.state({"eff": 0.2, "unit": 0.001}, c) == "straight"
    assert curveexit.state({"eff": 0.5, "unit": 0.0031}, c) == "anomaly"  # over three typical minutes of volatility
    assert curveexit.state({"eff": 0.95, "unit": 0.001}, c) == "anomaly"  # outside its own range
    assert curveexit.state({"eff": 0.03, "unit": 0.001}, None) == "unknown"


def test_cuts_come_from_the_markets_own_history():
    ts, vs = series(wiggle(7200))
    got = curveexit.cuts_from(ts, vs, ts[-1])
    assert got and got["p33"] <= got["p67"] and got["n"] >= curveexit.MIN_SAMPLES
    short_ts, short_vs = series(wiggle(500))
    assert curveexit.cuts_from(short_ts, short_vs, short_ts[-1]) is None


def test_a_sale_and_flip_is_scored_against_what_the_bet_returned():
    sig = {"contracts": 10.0, "stake": 8.0, "bid": 0.5, "other_ask": 0.5}
    won = curveexit.score_signal(sig, actual=-8.0, other_won=True)  # the bet lost; the other side won
    assert won["sell"] < 0 and won["flip_change"] > won["sell_change"] and won["flip_change"] > 0
    lost = curveexit.score_signal(sig, actual=2.0, other_won=False)  # the bet won; the flip leg lost its cost
    assert lost["flip_change"] < lost["sell_change"] < 0


class Db:
    def __init__(self, rows, quote):
        self.rows, self.quote = rows, quote

    def execute(self, sql, args=()):
        data = self.quote if "FROM quotes" in sql else self.rows
        return SimpleNamespace(fetchone=lambda: data[0] if data else None, fetchall=lambda: data)


def test_a_signal_is_logged_once_for_an_open_bet_inside_its_last_five_minutes(tmp_path, monkeypatch):
    now = 1_900_000_000.0
    vals = wiggle(1500) + [100.0 * math.exp(-0.002 * k) for k in range(1, 6)]
    ts = [now - len(vals) + 1 + i for i in range(len(vals))]
    from kalshi_moonshot import marketprofile, ticks

    monkeypatch.setattr(marketprofile, "_index", lambda series: "IDX")
    monkeypatch.setattr(ticks.history, "window", lambda idx, a, b: (ts, vals))
    close = "2026-03-17T17:46:40+00:00"  # 1_900_000_000 + 120 s
    from datetime import datetime

    now = datetime.fromisoformat(close).timestamp() - 120.0
    ts[:] = [now - len(vals) + 1 + i for i in range(len(vals))]
    row = {"ticker": "KXTEST15M-X-00", "lane": "lane_a", "side": "yes", "close_time": close, "contracts": 10.0, "stake_usd": 8.0}
    store = SimpleNamespace(open_bets=lambda: [row])
    tape = Db([], [{"bid": 0.55, "ask": 0.57, "ts": now - 1.0}])
    specs = {"lane_a": SimpleNamespace(curve_shadow=True)}
    s = SimpleNamespace(data_dir=str(tmp_path))
    curveexit._ran_at, curveexit._scored_at = 0.0, 0.0
    curveexit._seen.clear()
    got = curveexit.shadow_pass(s, store, tape, specs, now=now)
    assert len(got) == 1 and got[0]["bid"] == 0.55 and abs(got[0]["other_ask"] - 0.45) < 1e-9 and got[0]["left"] == 120.0
    assert json.loads((tmp_path / "curve_shadow.jsonl").read_text().splitlines()[0])["kind"] == "signal"
    assert curveexit.shadow_pass(s, store, tape, specs, now=now + 6.0) == []  # once per bet
    curveexit._seen.clear()
    off = {"lane_a": SimpleNamespace(curve_shadow=False)}
    assert curveexit.shadow_pass(s, store, tape, off, now=now + 12.0) == []
