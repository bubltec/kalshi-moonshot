"""The shock exit on the contract's own price (quoteshock.py, lane shock_source = "quote"): a market with no spot feed."""

import math

from kalshi_moonshot import config, quoteshock, ticks, watcher
from kalshi_moonshot.store import Store

CLOSE = 2_000_000.0


def _rows(start, end, mid_of):
    return [(float(t), mid_of(t)) for t in range(int(start), int(end) + 1)]


def _jitter(base):
    return lambda t: base + (0.004 if t % 2 else -0.004)


def test_the_held_mid_is_the_yes_mid_or_one_less():
    assert quoteshock.held(0.7, "yes") == 0.7 and math.isclose(quoteshock.held(0.7, "no"), 0.3)


def test_a_typical_minute_needs_five_minutes_of_moving_quotes():
    now = 10_000.0
    assert quoteshock.typical_minute(_rows(now - 200, now, _jitter(0.6)), now) is None  # too short
    assert quoteshock.typical_minute(_rows(now - 600, now, lambda t: 0.6), now) is None  # flat
    assert quoteshock.typical_minute(_rows(now - 600, now - 30, _jitter(0.6)), now) is None  # stale
    unit = quoteshock.typical_minute(_rows(now - 600, now, _jitter(0.6)), now)
    assert unit is not None and math.isclose(unit, 0.008 * math.sqrt(60.0), rel_tol=0.02)  # an 0.008 flip every second


def test_a_reading_is_in_per_mille_with_the_held_side_ahead_positive():
    now = 10_000.0
    rows = _rows(now - 600, now - 60, _jitter(0.80)) + _rows(now - 59, now, lambda t: 0.55)
    r = quoteshock.read(rows, "yes", now, 60.0)
    assert not isinstance(r, str)
    assert math.isclose(r.margin, 50.0) and r.move < -200.0 and r.unit > 0
    n = quoteshock.read(rows, "no", now, 60.0)  # the same market seen from a NO holder: ahead, and gaining
    assert math.isclose(n.margin, -50.0) and n.move > 200.0


def test_a_reading_says_why_it_cannot_be_read():
    now = 10_000.0
    assert isinstance(quoteshock.read([], "yes", now, 60.0), str)
    assert "old" in quoteshock.read(_rows(now - 600, now - 30, _jitter(0.6)), "yes", now, 60.0)
    assert "look-back" in quoteshock.read(_rows(now - 20, now, _jitter(0.6)), "yes", now, 60.0)


def _watcher(tmp_path, monkeypatch, side="yes"):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(
        "[lane.tq]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nshock_source='quote'\nshock_units=1.0\nshock_margin_units=0.6\nshock_seconds=60\nshock_last=300\nshock_memory=0\n"
        "burst_exit=false\nseries=['KX10YRRATE15M']\nalways=true\n"
    )
    s = config.load()
    store = Store(s.db_path)
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1.0, "KX10YRRATE15M-1", "bet", "observe", 0.82, side, "tq", 3, 2.7, "1970-01-24T03:33:20Z"),
    )
    store.db.commit()

    class Client:
        def market(self, ticker):
            return {"ticker": ticker, "floor_strike": 4.0, "close_time": "1970-01-24T03:33:20Z", "yes_bid_dollars": "0.82", "yes_ask_dollars": "0.83", "title": "t"}

    ticks.mids.clear()
    w = watcher.ProfitWatcher(lambda: s, Client(), lambda: Store(s.db_path))
    w.refresh()
    return w


def _feed(ticker, rows):
    for ts, mid in rows:
        ticks.mids.note(ticker, ts, mid - 0.005, mid + 0.005)


def _late(tmp_path, monkeypatch, bids, units=1.0):
    w = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KX10YRRATE15M-1", "tq")]
    t.late_units, t.late_seconds, t.close_ts = units, 30.0, CLOSE
    t.history.clear()
    hit = None
    for sec, b in bids:
        hit = w._late_drop_hit(t, b, float(sec))
        if hit:
            return hit, sec
    return None, None

