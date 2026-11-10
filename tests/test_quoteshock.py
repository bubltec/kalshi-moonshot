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
        "settlement_guard=0\nburst_exit=false\nseries=['KX10YRRATE15M']\nalways=true\n"
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


def test_a_lane_with_a_quote_source_arms_with_no_index_and_no_reference_price(tmp_path, monkeypatch):
    w = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KX10YRRATE15M-1", "tq")]
    assert t.shock_source == "quote" and t.shock_units == 1.0 and t.shock_margin_units == 0.6 and t.shock_index == "" and t.cross_units < 0


def test_the_quote_shock_sells_a_fast_fall_into_a_contested_price(tmp_path, monkeypatch):
    w = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KX10YRRATE15M-1", "tq")]
    now = CLOSE - 120
    _feed(t.ticker, _rows(now - 700, now - 60, _jitter(0.80)) + _rows(now - 59, now, lambda s: 0.50))
    why = w._shock_hit(t, 0.49, now, now)
    assert why and why.startswith("shock exit") and "contract's mid" in why


def test_the_quote_shock_leaves_a_fall_that_ends_far_from_even_and_a_quiet_market(tmp_path, monkeypatch):
    w = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KX10YRRATE15M-1", "tq")]
    now = CLOSE - 120
    _feed(t.ticker, _rows(now - 700, now - 60, _jitter(0.95)) + _rows(now - 59, now, lambda s: 0.80))
    assert w._shock_hit(t, 0.79, now, now) is None  # a big fall, but the side is still well ahead
    ticks.mids.clear()
    _feed(t.ticker, _rows(now - 700, now, _jitter(0.50)))
    assert w._shock_hit(t, 0.49, now, now) is None  # contested but not moving against us


def test_the_quote_shock_does_nothing_without_enough_quote_history_or_outside_its_window(tmp_path, monkeypatch):
    w = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KX10YRRATE15M-1", "tq")]
    now = CLOSE - 120
    _feed(t.ticker, _rows(now - 100, now - 60, _jitter(0.80)) + _rows(now - 59, now, lambda s: 0.50))
    assert w._shock_hit(t, 0.49, now, now) is None  # under five minutes of quotes: the typical minute cannot be read
    ticks.mids.clear()
    early = CLOSE - 400
    _feed(t.ticker, _rows(early - 700, early - 60, _jitter(0.80)) + _rows(early - 59, early, lambda s: 0.50))
    assert w._shock_hit(t, 0.49, early, early) is None  # earlier than shock_last (300 s)


def test_a_no_holder_sells_when_the_yes_mid_jumps_up(tmp_path, monkeypatch):
    w = _watcher(tmp_path, monkeypatch, side="no")
    t = w.targets[("KX10YRRATE15M-1", "tq")]
    now = CLOSE - 120
    _feed(t.ticker, _rows(now - 700, now - 60, _jitter(0.20)) + _rows(now - 59, now, lambda s: 0.50))
    assert (w._shock_hit(t, 0.49, now, now) or "").startswith("shock exit")


def test_shock_persist_holds_the_sale_until_the_condition_has_lasted(tmp_path, monkeypatch):
    w = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KX10YRRATE15M-1", "tq")]
    t.shock_persist, t.shock_memory = 20.0, 120.0
    now = CLOSE - 150
    _feed(t.ticker, _rows(now - 700, now - 60, _jitter(0.80)) + _rows(now - 59, now, lambda s: 0.50))
    assert w._shock_hit(t, 0.49, now, now) is None  # the run starts
    _feed(t.ticker, _rows(now + 1, now + 10, lambda s: 0.50))
    assert w._shock_hit(t, 0.49, now + 10, now + 10) is None  # 10 s: not yet
    _feed(t.ticker, _rows(now + 11, now + 20, lambda s: 0.50))
    why = w._shock_hit(t, 0.49, now + 20, now + 20)  # 20 s: sold
    assert why and why.startswith("shock exit")


def test_shock_persist_restarts_when_the_condition_breaks_or_the_readings_gap(tmp_path, monkeypatch):
    w = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KX10YRRATE15M-1", "tq")]
    t.shock_persist, t.shock_memory = 20.0, 120.0
    now = CLOSE - 150
    _feed(t.ticker, _rows(now - 700, now - 60, _jitter(0.80)) + _rows(now - 59, now, lambda s: 0.50))
    assert w._shock_hit(t, 0.49, now, now) is None
    _feed(t.ticker, _rows(now + 1, now + 10, lambda s: 0.50))
    assert w._shock_hit(t, 0.49, now + 10, now + 10) is None
    _feed(t.ticker, _rows(now + 11, now + 14, lambda s: 0.50))
    assert w._shock_hit(t, 0.93, now + 14, now + 14) is None  # the bid left the band: the run is over
    _feed(t.ticker, _rows(now + 15, now + 24, lambda s: 0.50))
    assert w._shock_hit(t, 0.49, now + 24, now + 24) is None  # a new run began here, 0 s old
    _feed(t.ticker, _rows(now + 25, now + 40, lambda s: 0.50))
    assert w._shock_hit(t, 0.49, now + 40, now + 40) is None  # 16 s without a reading: the run restarted here
    _feed(t.ticker, _rows(now + 41, now + 50, lambda s: 0.50))
    assert w._shock_hit(t, 0.49, now + 50, now + 50) is None
    _feed(t.ticker, _rows(now + 51, now + 60, lambda s: 0.50))
    assert (w._shock_hit(t, 0.49, now + 60, now + 60) or "").startswith("shock exit")  # 20 s after that restart


def test_a_lane_without_shock_persist_sells_on_the_first_reading(tmp_path, monkeypatch):
    w = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KX10YRRATE15M-1", "tq")]
    assert t.shock_persist == 0.0
    now = CLOSE - 120
    _feed(t.ticker, _rows(now - 700, now - 60, _jitter(0.80)) + _rows(now - 59, now, lambda s: 0.50))
    assert (w._shock_hit(t, 0.49, now, now) or "").startswith("shock exit")
