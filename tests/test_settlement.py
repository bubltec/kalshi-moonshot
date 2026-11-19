"""The last minute of a 15-minute crypto market: the settlement average, and the guard."""
import json
import math
import queue
import time

import pytest

from kalshi_moonshot import config, settlement, watcher
from kalshi_moonshot.store import Store


def test_far_from_the_close_the_chance_is_the_spots_side_of_the_strike():
    up = settlement.chance_above(100.0, 100.5, 0.0, 120.0, 0.0001)
    down = settlement.chance_above(100.0, 99.5, 0.0, 120.0, 0.0001)
    assert up > 0.5 > down and up + down == pytest.approx(1.0, abs=1e-6)  # symmetric around the strike


def test_a_locked_part_of_the_average_pulls_the_answer_toward_it():
    # 20 seconds left: 40 seconds of the average are past. The spot is 1.0 over the strike now, but those 40 seconds averaged 3.0 under it.
    strike, spot, sigma = 100.0, 101.0, 0.00005
    p_locked = settlement.chance_above(strike, spot, 97.0, 20.0, sigma)
    p_fresh = settlement.chance_above(strike, spot, 101.0, 20.0, sigma)
    assert p_locked < 0.01 and p_fresh > 0.99  # the same spot, a very different answer: the average is what pays


def test_a_fractional_time_left_does_not_move_the_answer_by_a_whole_tick():
    """The live bug: with 58.4 s left only 1.6 s of the average has passed. Weighting by the exact elapsed fraction keeps the answer continuous; counting whole-second ticks jumped by 1/60 of the price (many standard deviations)."""
    strike, spot, sigma = 2344.11, 2342.15, 3.9e-5
    answers = [settlement.chance_above(strike, spot, 2342.2, left, sigma) for left in (59.6, 59.0, 58.4, 57.7, 57.0)]
    assert all(a is not None and a < 1e-3 for a in answers)  # the spot is under the target the whole time: a certain NO, however the seconds are cut


def test_a_bad_input_gives_none():
    assert settlement.chance_above(0, 100.0, 0.0, 30.0, 0.0001) is None
    assert settlement.chance_above(100.0, 100.0, 0.0, 0.0, 0.0001) is None
    assert settlement.chance_above(100.0, 100.0, 0.0, 30.0, 0.0) is None


def test_volatility_and_the_locked_average_from_ticks():
    ts = [float(t) for t in range(2000)]
    vals = [100.0 * math.exp(0.0001 * math.sin(t / 7.0)) for t in ts]
    sigma = settlement.sigma_per_second(ts, vals, 1999.0)
    assert sigma is not None and 1e-6 < sigma < 1e-3
    got = settlement.locked_average(ts, vals, 1500.0, 1490.0)  # now = 1490: seconds 1440..1489 are past (50 of them)
    assert got == pytest.approx(sum(vals[1440:1490]) / 50, rel=1e-9)
    assert settlement.locked_average(ts, vals, 1500.0, 1440.7) == pytest.approx(vals[1440], rel=1e-9)  # one whole second past
    assert settlement.sigma_per_second([], [], 100.0) is None


class _Tape:
    """Index ticks as the watcher reads them: a flat spot, then a fall under the strike in the final minute."""

    def __init__(self, close, fall_at):
        self.close, self.fall_at = close, fall_at

    def execute(self, sql, params=()):
        _index_id, lo, hi = params[0], params[1], params[2]
        rows = []
        t = int(lo) + 1
        while t <= hi:
            v = 100.0 + (0.0 if t < self.fall_at else -0.1 * (t - self.fall_at))
            rows.append((float(t), v + 0.01 * ((t % 7) - 3) + 0.003 * ((t // 10) % 3)))
            t += 1
        return type("R", (), {"fetchall": lambda self_, rows=rows: rows})()


def _watcher(tmp_path, monkeypatch, guard=0.10, ticker="KXBTC15M-1"):
    series = ticker.split("-")[0]
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(f"[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nsettlement_guard={guard}\nburst_exit=true\nseries=['{series}']\nalways=true\n")
    s = config.load()
    store = Store(s.db_path)
    close = 2_000_000.0
    store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)", (1.0, ticker, "bet", "observe", 0.90, "yes", "guarded", 3, 2.7, "1970-01-24T03:33:20Z"))
    store.db.commit()

    class Client:
        def market(self, ticker):
            return {"ticker": ticker, "floor_strike": 100.0, "close_time": "1970-01-24T03:33:20Z", "yes_bid_dollars": "0.90", "yes_ask_dollars": "0.91", "title": "t"}

    w = watcher.ProfitWatcher(lambda: s, Client(), lambda: Store(s.db_path))
    w.refresh()
    return w, s, store, close


def _through_watcher(tmp_path, monkeypatch, sale=0.003, seconds=60, rearm=False):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(
        "[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\n"
        f"strike_through_seconds={seconds}\nstrike_through_rearm={str(rearm).lower()}\nsettlement_guard=0\nburst_exit=false\n"
        "series=['KXBTC15M']\nalways=true\n"
    )
    s = config.load()
    store = Store(s.db_path)
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1.0, "KXBTC15M-1", "bet", "observe", 0.90, "yes", "guarded", 3, 2.7, "1970-01-24T03:33:20Z"),
    )
    store.db.commit()

    class Client:
        def market(self, ticker):
            return {"ticker": ticker, "floor_strike": 100.0, "close_time": "1970-01-24T03:33:20Z", "yes_bid_dollars": "0.90", "yes_ask_dollars": "0.91", "title": "t"}

    w = watcher.ProfitWatcher(lambda: s, Client(), lambda: Store(s.db_path))
    w.refresh()
    return w, s, store, 2_000_000.0


def _clear_through(t):
    t.through_kept = t.through_on = t.through_bounce = False
    t.through_worst = t.through_best = t.through_best_ts = t.through_since = 0.0


def _shock_watcher(tmp_path, monkeypatch, extra=""):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(
        "[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nshock_bp=8\nshock_seconds=60\nshock_margin_bp=5\nshock_last=120\n"
        f"burst_exit=false\nseries=['KXBTC15M']\nalways=true\n{extra}"
    )
    s = config.load()
    store = Store(s.db_path)
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (1.0, "KXBTC15M-1", "bet", "observe", 0.82, "yes", "guarded", 3, 2.7, "1970-01-24T03:33:20Z"),
    )
    store.db.commit()

    class Client:
        def market(self, ticker):
            return {"ticker": ticker, "floor_strike": 100.0, "close_time": "1970-01-24T03:33:20Z", "yes_bid_dollars": "0.82", "yes_ask_dollars": "0.83", "title": "t"}

    w = watcher.ProfitWatcher(lambda: s, Client(), lambda: Store(s.db_path))
    w.refresh()
    return w, 2_000_000.0


def _wrong_side_world(tmp_path, monkeypatch, before, extra="", varieties="wrong_side_varieties=['G']\n"):
    """A KXCOPPER15M YES bought 437 s before the close; `before` is the 5 minutes of BiQuote ticks ahead of the buy as (seconds before the close, value)."""
    from kalshi_moonshot import db

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(
        "[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nwrong_side_bp=5\nwrong_side_seconds=15\n"
        f"{varieties}{extra}burst_exit=false\nstop_loss=false\nwatch_loss=0\nseries=['KXCOPPER15M']\nalways=true\n"
    )
    s = config.load()
    close = 2_000_000.0
    store = Store(s.db_path)
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (close - 437.0, "KXCOPPER15M-1", "bet", "trade", 0.83, "yes", "guarded", 3, 2.7, "1970-01-24T03:33:20Z"),
    )
    store.db.commit()

    class Client:
        def market(self, ticker):
            return {"ticker": ticker, "floor_strike": 100.0, "close_time": "1970-01-24T03:33:20Z", "yes_bid_dollars": "0.80", "yes_ask_dollars": "0.81", "title": "t"}

    tape = db.connect(s.tape_path, "tape", shared=True)
    tape.execute("CREATE TABLE IF NOT EXISTS index_ticks (ts REAL NOT NULL, index_id TEXT NOT NULL, value REAL, avg60 REAL, quarter_avg REAL, src_ms INTEGER)")
    tape.execute("INSERT INTO index_ticks (ts, index_id, value) VALUES (?,?,?)", (close - 897.0, "BQ_COPPER", 100.0))  # the price at the window open
    for sec, value in before:
        tape.execute("INSERT INTO index_ticks (ts, index_id, value) VALUES (?,?,?)", (close - sec, "BQ_COPPER", value))
    tape.commit()
    w = watcher.ProfitWatcher(lambda: s, Client(), lambda: Store(s.db_path))
    w.tape_factory = lambda: db.connect(s.tape_path, "tape", shared=True)
    w.refresh()
    return w, close


def _feed(w, close, sec, value, bid):
    w.on_event({"kind": "quote", "ticker": "KXCOPPER15M-1", "bid": bid, "ask": bid + 0.01, "ts": close - sec - 0.2})
    w.on_event({"kind": "index", "index_id": "BQ_COPPER", "ts": close - sec, "value": value})


def _fade_after_a_shock(w, close, sell_expected):
    """A YES whose spot sits 20 bp over the strike, drops 19 bp between 200 and 190 s left (a shock) and then sits 1 bp over it, bid 0.55."""
    t = w.targets[("KXBTC15M-1", "guarded")]
    fired = None
    for left in range(250, 4, -2):
        value = 100.20 if left > 200 else 100.01 if left < 190 else 100.20 - 0.019 * (200 - left)
        w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.55, "ask": 0.56, "ts": close - left - 0.2})
        w.on_event({"kind": "index", "index_id": "BRTI", "ts": close - left, "value": value})
        if not w._jobs.empty():
            fired = (left, w._jobs.get_nowait().why)
            break
    assert (fired is not None) == sell_expected
    return t, fired


def _quiet_minute(w, now, per_second=40, ticker="KXBTC15M-1"):
    """Two minutes of ordinary trading before `now`: 40 contracts a second, half and half."""
    for k in range(125, 4, -1):
        w.on_event({"kind": "trade", "ticker": ticker, "ts": now - k, "count": per_second / 2, "taker": "yes", "price": 0.9})
        w.on_event({"kind": "trade", "ticker": ticker, "ts": now - k, "count": per_second / 2, "taker": "no", "price": 0.9})


def _buy_inline(w):
    """In production the buy worker takes a hunt buy off the queue; in a test it runs on the spot, so the row (or its absence) can be read right after the trade."""

    class Inline(queue.SimpleQueue):
        def put(self, job, block=True, timeout=None):
            super().put(job)
            w._drain_buys()

    w._buys = Inline()


def _hunt_watcher(tmp_path, monkeypatch, ticker="KXBTC15M-1"):
    series = ticker.split("-")[0]
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(
        f"[lane.crypto_moonshot]\nestimator='ladder'\nprice=[0.02,0.10]\nhours=[0.0,0.04]\nalways=true\n"
        f"burst_hunt=true\nseries=['{series}']\nsides=['yes','no']\nfixed_stake=2.0\nstop_loss=false\n"
    )
    s = config.load()
    store = Store(s.db_path)
    close = 2_000_000.0

    class Client:
        def market(self, ticker):
            return {"ticker": ticker, "floor_strike": 100.0, "close_time": "1970-01-24T03:33:20Z", "yes_bid_dollars": "0.90", "yes_ask_dollars": "0.91", "title": "t"}

    w = watcher.ProfitWatcher(lambda: s, Client(), lambda: Store(s.db_path))
    w.refresh()
    _buy_inline(w)
    return w, s, store, close


def _sat_cheap(w, at, bid=0.90, ask=0.91, ticker="KXBTC15M-1"):
    """The cheap side's ask, every second for the 20s before `at`."""
    for k in range(21):
        w.on_event({"kind": "quote", "ticker": ticker, "bid": bid, "ask": ask, "ts": at - 20 + k})


























def test_the_settlement_edge_logs_the_underpriced_side_once_per_bar(tmp_path, monkeypatch):
    from kalshi_moonshot import settlement_edge

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    store = Store(s.db_path)
    close = 2_000_000.0

    class Client:
        def market(self, ticker):
            return {"floor_strike": 100.0, "close_time": "1970-01-24T03:33:20Z"}

    edge = settlement_edge.SettlementEdge(lambda: s, Client(), lambda: Store(s.db_path), lambda: _Tape(close, fall_at=close + 999))
    now = close - 100
    edge.on_event({"kind": "index", "index_id": "BRTI", "ts": now, "value": 100.0})  # the spot sits on the strike: the exact chance is about one half
    edge.on_event({"kind": "quote", "ticker": "KXBTC15M-26OCT250100-00", "bid": 0.10, "ask": 0.12, "ts": now})  # but the YES side costs 12 cents
    assert edge.evaluate(now) == 3  # a gap of about 38 points clears all three bars: one paper bet each
    assert edge.evaluate(now + 0.5) == 0  # each bar is logged once per market
    rows = store.db.execute("SELECT side, ask, model_p, edge, action, context FROM decisions WHERE lane='settlement_edge' ORDER BY id").fetchall()
    assert [r["side"] for r in rows] == ["yes", "yes", "yes"] and rows[0]["ask"] == pytest.approx(0.12) and rows[0]["model_p"] > 0.4
    assert sorted(json.loads(r["context"])["bar"] for r in rows) == [0.05, 0.1, 0.15]


def test_the_settlement_edge_stays_quiet_outside_the_window_or_without_a_spot(tmp_path, monkeypatch):
    from kalshi_moonshot import settlement_edge

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    Store(s.db_path)
    close = 2_000_000.0

    class Client:
        def market(self, ticker):
            return {"floor_strike": 100.0, "close_time": "1970-01-24T03:33:20Z"}

    edge = settlement_edge.SettlementEdge(lambda: s, Client(), lambda: Store(s.db_path), lambda: _Tape(close, fall_at=close + 999))
    far = close - 400
    edge.on_event({"kind": "index", "index_id": "BRTI", "ts": far, "value": 100.0})
    edge.on_event({"kind": "quote", "ticker": "KXBTC15M-26OCT250100-00", "bid": 0.10, "ask": 0.12, "ts": far})
    assert edge.evaluate(far) == 0  # 400 s left: outside the last 150
    near = close - 100
    edge.on_event({"kind": "quote", "ticker": "KXBTC15M-26OCT250100-00", "bid": 0.10, "ask": 0.12, "ts": near})
    assert edge.evaluate(near) == 0  # no index tick yet: no chance, no log






def test_side_trend_is_the_move_of_the_sides_mid_over_the_window(tmp_path):
    from kalshi_moonshot import db, runner
    from kalshi_moonshot.strategy import LongShot

    class S:
        tape_path = str(tmp_path / "tape.db")

    tape = db.connect(S.tape_path, "tape", shared=True)
    tape.execute("CREATE TABLE IF NOT EXISTS quotes (ts REAL NOT NULL, ticker TEXT NOT NULL, bid REAL, ask REAL, last REAL, sts INTEGER, bid_size REAL, ask_size REAL)")
    for ts, bid, ask in ((100.0, 0.78, 0.80), (115.0, 0.80, 0.82), (130.0, 0.76, 0.78)):
        tape.execute("INSERT INTO quotes (ts, ticker, bid, ask) VALUES (?,?,?,?)", (ts, "KXBTC15M-1", bid, ask))
    tape.commit()
    yes = LongShot("KXBTC15M-1", "", "", 0.76, 0.78, 0.0, "", 0.1, side="yes")
    no = LongShot("KXBTC15M-1", "", "", 0.22, 0.24, 0.0, "", 0.1, side="no")
    assert runner.side_trend(S, yes, 30.0, now=131.0) == pytest.approx(-0.02)  # mid 0.79 at 100, 0.77 at 130: it fell 2c
    assert runner.side_trend(S, no, 30.0, now=131.0) == pytest.approx(0.02)  # the NO side rose
    assert runner.side_trend(S, yes, 30.0, now=500.0) is None  # no recent quote: no opinion


def test_trend_in_the_markets_own_units(tmp_path):
    from kalshi_moonshot import db, runner
    from kalshi_moonshot.strategy import LongShot

    class S:
        tape_path = str(tmp_path / "tape.db")

    tape = db.connect(S.tape_path, "tape", shared=True)
    tape.execute("CREATE TABLE IF NOT EXISTS quotes (ts REAL NOT NULL, ticker TEXT NOT NULL, bid REAL, ask REAL, last REAL, sts INTEGER, bid_size REAL, ask_size REAL)")
    for k in range(160):  # a market that wobbles by a cent, then falls 6c over its last 10 seconds
        mid = 0.80 + (0.01 if k % 2 else 0.0) - (0.006 * max(0, k - 150))
        tape.execute("INSERT INTO quotes (ts, ticker, bid, ask) VALUES (?,?,?,?)", (1000.0 + k, "KXBTC15M-1", mid - 0.01, mid + 0.01))
    tape.commit()
    yes = LongShot("KXBTC15M-1", "", "", 0.80, 0.82, 0.0, "", 0.1, side="yes")
    no = LongShot("KXBTC15M-1", "", "", 0.18, 0.20, 0.0, "", 0.1, side="no")
    zy = runner.side_trend_z(S, yes, 10.0, now=1160.0)
    assert zy is not None and zy < -1.5  # six cents down against a market whose usual 10 s change is about a cent
    zn = runner.side_trend_z(S, no, 10.0, now=1160.0)
    assert zn is not None and zn == pytest.approx(-zy, rel=1e-6)  # the NO side is the mirror
    assert runner.side_trend_z(S, yes, 10.0, now=5000.0) is None  # no recent quote


def _variety_world(tmp_path, monkeypatch, ticks, extra="skip_varieties=['G', 'A-yes']"):
    import time as _time

    from kalshi_moonshot import db, regime

    regime.reset()
    w, s, store, _close = _watcher(tmp_path, monkeypatch)
    (tmp_path / "strategies.toml").write_text(f"[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=2.0\nseries=['KXCOPPER15M']\n{extra}\n")
    s = config.load()
    tape = db.connect(s.tape_path, "tape", shared=True)
    tape.execute("CREATE TABLE IF NOT EXISTS index_ticks (ts REAL NOT NULL, index_id TEXT NOT NULL, value REAL, avg60 REAL, quote_avg REAL, src_ms INTEGER)")
    now = _time.time()
    close = now + 437.0
    tape.execute("INSERT INTO index_ticks (ts, index_id, value) VALUES (?,?,?)", (close - 897.0, "BQ_COPPER", 100.0))  # the price at the window open
    for k, value in enumerate(ticks):
        tape.execute("INSERT INTO index_ticks (ts, index_id, value) VALUES (?,?,?)", (now - 302.0 + k * 2.0, "BQ_COPPER", value))
    tape.commit()
    return w, s, store


def _copper_shot(side="yes", ask=0.86):
    from kalshi_moonshot.strategy import LongShot

    return LongShot("KXCOPPER15M-9", "t", "", ask - 0.01, ask, 437.0 / 3600.0, "2099-01-01T00:00:00Z", 437.0 / 3600.0, lane="guarded", series="KXCOPPER15M", side=side)


def _choppy_world(tmp_path, monkeypatch, lane_extra):
    import time as _time

    from kalshi_moonshot import db, regime

    regime.reset()
    w, s, store, _close = _watcher(tmp_path, monkeypatch)
    (tmp_path / "strategies.toml").write_text(f"[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=2.0\nseries=['KXBTC15M']\n{lane_extra}\n")
    s = config.load()
    tape = db.connect(s.tape_path, "tape", shared=True)
    tape.execute("CREATE TABLE IF NOT EXISTS quotes (ts REAL NOT NULL, ticker TEXT NOT NULL, bid REAL, ask REAL, last REAL, sts INTEGER, bid_size REAL, ask_size REAL)")
    now = _time.time()
    for k in range(14):  # 14 settled windows whose YES favorite (ask 0.85) won only 4 times
        close = int(now - 600 - 900 * k)
        ticker = f"KXBTC15M-{k}"
        store.db.execute("INSERT INTO windows (ticker, series, close_ts, result, fetched_ts) VALUES (?,?,?,?,?)", (ticker, "KXBTC15M", close, "yes" if k < 4 else "no", now))
        tape.execute("INSERT INTO quotes (ts, ticker, bid, ask) VALUES (?,?,?,?)", (close - 300.0, ticker, 0.84, 0.85))
    tape.commit()
    store.db.commit()
    return w, s, store


def _choppy_shot(left_seconds, ask):
    from kalshi_moonshot.strategy import LongShot

    return LongShot("KXBTC15M-99", "t", "", ask - 0.01, ask, left_seconds / 3600.0, "2099-01-01T00:00:00Z", left_seconds / 3600.0, lane="guarded", series="KXBTC15M", side="yes")


def test_a_postgres_connection_serves_one_thread_at_a_time():
    import threading
    import time

    from kalshi_moonshot.db import PgConn

    inside, worst = [0], [0]

    class Slow:
        closed = False
        broken = False

        def execute(self, *a, **k):
            inside[0] += 1
            worst[0] = max(worst[0], inside[0])
            time.sleep(0.01)
            inside[0] -= 1

            class Cur:
                description = None
                rowcount = 0

                def fetchall(self):
                    return []

                def fetchone(self):
                    return None
            return Cur()

        def rollback(self):
            pass

        def commit(self):
            pass

    c = PgConn.__new__(PgConn)
    c.dsn, c.schema, c._dirty, c._pk, c.conn, c._lock = "", "x", False, {}, Slow(), threading.RLock()
    ts = [threading.Thread(target=lambda: [c.execute("SELECT 1") for _ in range(5)]) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert worst[0] == 1  # never two statements in flight on the one connection


def test_a_paper_bet_buys_the_way_the_live_lane_does(tmp_path, monkeypatch):
    from kalshi_moonshot import db, paperfill

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.resting]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=2.0\nseries=['KXBTC15M']\nbuy_rest_seconds=20\nbuy_rest_below=0.01\n")
    s = config.load()
    store = Store(s.db_path)
    tape = db.connect(str(tmp_path / "tape.db"), "tape", shared=True)
    tape.execute("CREATE TABLE IF NOT EXISTS quotes (ts REAL NOT NULL, ticker TEXT NOT NULL, bid REAL, ask REAL, last REAL, sts INTEGER, bid_size REAL, ask_size REAL)")

    tape.execute("CREATE TABLE IF NOT EXISTS flow_1s (ts INTEGER NOT NULL, ticker TEXT NOT NULL, yes_vol REAL NOT NULL, no_vol REAL NOT NULL, n INTEGER NOT NULL, vwap REAL)")

    def bet(tk, ts):
        store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, reason) VALUES (?,?,?,?,?,?,?,?,?,?)", (ts, tk, "bet", "observe", 0.80, "yes", "resting", 2.5, 2.0, "x"))

    def quotes(tk, rows):
        for ts, bid, ask in rows:
            tape.execute("INSERT INTO quotes (ts, ticker, bid, ask) VALUES (?,?,?,?)", (ts, tk, bid, ask))

    bet("KXBTC15M-RUNS", 1000.0)  # the ask never comes down: the bid misses it
    quotes("KXBTC15M-RUNS", [(999.0, 0.78, 0.80), (1005.0, 0.80, 0.82), (1015.0, 0.85, 0.87)])
    bet("KXBTC15M-DIPS", 1000.0)  # the ask comes down to our bid inside the window: it fills
    quotes("KXBTC15M-DIPS", [(999.0, 0.78, 0.80), (1008.0, 0.77, 0.79)])
    bet("KXBTC15M-CROSS", 1000.0)  # the ask had already moved under our bid when the order went in: refused
    quotes("KXBTC15M-CROSS", [(999.0, 0.76, 0.78), (1002.0, 0.76, 0.78)])
    bet("KXBTC15M-SOLD", 1000.0)  # the ask stays up, but a seller hits our bid at 0.79: it fills
    quotes("KXBTC15M-SOLD", [(999.0, 0.78, 0.80), (1010.0, 0.78, 0.80)])
    tape.execute("INSERT INTO flow_1s (ts, ticker, yes_vol, no_vol, n, vwap) VALUES (?,?,?,?,?,?)", (1006, "KXBTC15M-SOLD", 0.0, 12.0, 2, 0.79))
    store.db.commit()
    got = paperfill.settle(s, store, tape, now=1100.0)
    assert got == {"filled": 2, "missed": 2}
    rows = {r["ticker"]: r for r in store.db.execute("SELECT ticker, action, ask, stake_usd FROM decisions").fetchall()}
    assert rows["KXBTC15M-DIPS"]["action"] == "bet" and rows["KXBTC15M-DIPS"]["ask"] == pytest.approx(0.79)
    assert rows["KXBTC15M-RUNS"]["action"] == "pass" and rows["KXBTC15M-RUNS"]["stake_usd"] is None
    assert rows["KXBTC15M-CROSS"]["action"] == "pass"
    assert rows["KXBTC15M-SOLD"]["action"] == "bet"
    assert paperfill.settle(s, store, tape, now=1200.0) == {"filled": 0, "missed": 0}  # each bet is judged once


def test_the_gap_report_puts_paper_and_live_side_by_side(tmp_path, monkeypatch):
    from kalshi_moonshot import gap

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    store = Store(s.db_path)
    for i, (mode, side) in enumerate((("trade", "yes"), ("trade", "yes"), ("observe", "yes"), ("observe", "yes"))):
        tk = f"KXBTC15M-{i}"
        store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, lane_ver) VALUES (?,?,?,?,?,?,?,?,?,?)", (time.time() - 60, tk, "bet", mode, 0.80, side, "x", 2.0, 1.6, "v1"))
        store.db.execute("INSERT INTO windows (ticker, series, close_ts, result, fetched_ts) VALUES (?,?,?,?,?)", (tk, "KXBTC15M", 1000.0 + i, "yes" if i != 1 else "no", 1.0))
    store.db.commit()
    text = gap.report(store, hours=1)
    assert "x  (version v1)" in text and "live    2 bets" in text and "paper   2 bets" in text and "paper wins +50 points more than live" in text


def test_best_bid_reads_the_side_of_the_book_it_sells_into():
    from kalshi_moonshot.strategy import best_bid

    book = {"orderbook_fp": {"yes_dollars": [["0.40", "5"], ["0.61", "2"], ["0.70", "0"]], "no_dollars": [["0.30", "8"]]}}
    assert best_bid(book, "yes") == 0.61  # a zero-size row is not a bid
    assert best_bid(book, "no") == 0.30
    assert best_bid({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}, "yes") is None


def _queued_hunt(tmp_path, monkeypatch):
    """A hunt watcher with real queueing (no inline runner) and a lane that trades for real."""
    import dataclasses

    from kalshi_moonshot import lanes

    w, s, store, _close = _watcher(tmp_path, monkeypatch)
    spec = dataclasses.replace(lanes.lane_specs(s)["guarded"], name="guarded", fixed_stake=0.5, max_open=0)
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": True)
    w.client.authed = True
    h = watcher.Hunt("KXETH15M-1", "guarded", close_ts=time.time() + 100)
    return w, store, spec, h




























def _hunt_job(w, h, spec, side="no", ask=0.05):
    """What _on_hunt does when a trigger fires: mark the market fired and queue the buy."""
    h.fired = True
    w._buys.put(watcher.BuySignal("hunt", side, ask, "burst hunt: test", {}, hunt=h, spec=spec))


def _two_rows(store):
    return store.db.execute("SELECT id, mode, ask, contracts, stake_usd, reason, context FROM decisions WHERE ticker='KXETH15M-1'").fetchall()




















def _dollar_hunt(tmp_path, monkeypatch, *, yes_bid, yes_ask, left=60.0, baseline=0.0, flow=None, ticker="KXETH15M-1", **spec_changes):
    """A hunt on a YES-priced market with $455 bought against the favorite in the last 3 s, and the cheap side sitting at its ask for 20 s. Returns what _on_hunt queued."""
    import dataclasses

    from kalshi_moonshot import lanes

    w, s, _store, _close = _watcher(tmp_path, monkeypatch)
    fields = {"name": "guarded", "price": (0.02, 0.20), "hunt_dollars": 400.0, "sides": ("no",), "fixed_stake": 0.5, "max_open": 0, **spec_changes}
    spec = dataclasses.replace(lanes.lane_specs(s)["guarded"], **fields)
    now = 1_000_000.0
    sec = int(now)
    h = watcher.Hunt(ticker, "guarded", close_ts=now + left, yes_bid=yes_bid, yes_ask=yes_ask)
    h.quotes.extend((now - 20.0 + i, yes_bid, yes_ask) for i in range(21))
    if flow is None:
        book = {sec: [0.0, 6000.0, 0.0, 455.0]}
        for k in range(3, 123):  # the market's ordinary second on the cheap (NO) side: `baseline` dollars
            book[sec - k] = [0.0, 0.0, 0.0, baseline]
    else:
        book = {sec - k: row for k, row in flow.items()}
    w._on_hunt(h, spec, now, book, sec)
    return w._buys.qsize()


















def _book(yes_bids=(), no_bids=()):
    return {"orderbook_fp": {"yes_dollars": [[str(p), "100"] for p in yes_bids], "no_dollars": [[str(p), "100"] for p in no_bids]}}


def test_the_best_ask_of_a_side_is_one_minus_the_best_bid_of_the_other():
    from kalshi_moonshot.strategy import best_ask

    book = _book(yes_bids=(0.19, 0.20), no_bids=(0.78, 0.80))
    assert best_ask(book, "no") == pytest.approx(0.80)  # NO is offered at 1 - the best YES bid, 0.20
    assert best_ask(book, "yes") == pytest.approx(0.20)  # YES at 1 - the best NO bid, 0.80
    assert best_ask(_book(), "yes") is None and best_ask({}, "no") is None  # nobody bids the other side: no ask


def _resting(tmp_path, monkeypatch, *, book, shot_ask=0.82, fill_yes_price="0.2100", price_band=(0.76, 0.92), side="no"):
    """_place_resting against a fake exchange. Returns (what was sent, the Placed, the shot)."""
    import dataclasses

    from kalshi_moonshot import lanes, runner
    from kalshi_moonshot.strategy import LongShot

    _w, s, store, _close = _watcher(tmp_path, monkeypatch)
    monkeypatch.setattr(runner.time, "sleep", lambda x: None)

    class C:
        def __init__(self):
            self.sent = []

        def buy_resting(self, ticker, side, count, price, seconds, client_order_id=None, post_only=True):
            self.sent.append((side, count, price))
            return {"order_id": "o1"}

        def portfolio_rows(self, path, key, min_ts=None, pages=1):
            return [{"fill_id": "f1", "market_ticker": "KXBTC15M-9", "count_fp": "2", "yes_price_dollars": fill_yes_price, "fee_cost": "0", "ts": 1.0, "book_side": "ask", "is_taker": False, "order_id": "o1", "action": "buy", "side": side}]

    spec = dataclasses.replace(lanes.lane_specs(s)["guarded"], buy_rest_seconds=20.0, buy_rest_below=0.01, price=price_band)
    shot = LongShot("KXBTC15M-9", "t", "", shot_ask - 0.01, shot_ask, 10.0, "2099-01-01T00:00:00Z", 0.1, lane="guarded", series="KXBTC15M", side=side)
    c = C()
    out = runner._place_resting(s, c, store, shot, "why", 2.0, spec, None, book)
    return c.sent, out


def _resting_retry(tmp_path, monkeypatch, *, first_book, answers, fresh_book=None, fresh_error=False, shot_ask=0.83, order_key=None, ids=None):
    """_place_resting where the exchange answers each order from `answers` (an exception to refuse, a dict to accept). Returns (orders sent, Placed, book reads).
    Like Kalshi, it refuses a client_order_id it has already seen with 409 order_already_exists, even one on an order it rejected; `ids` collects the ids sent."""
    import dataclasses

    from kalshi_moonshot import lanes, runner
    from kalshi_moonshot.kalshi import KalshiError
    from kalshi_moonshot.strategy import LongShot

    _w, s, store, _close = _watcher(tmp_path, monkeypatch)
    monkeypatch.setattr(runner.time, "sleep", lambda x: None)
    reads = []

    class C:
        def __init__(self):
            self.sent = []

        def orderbook(self, ticker):
            reads.append(ticker)
            if fresh_error:
                raise KalshiError("GET orderbook: timeout")
            return fresh_book

        def buy_resting(self, ticker, side, count, price, seconds, client_order_id=None, post_only=True):
            self.sent.append(price)
            seen = [] if ids is None else ids
            if client_order_id is not None and client_order_id in seen:
                raise KalshiError('POST /portfolio/events/orders -> 409: {"error":{"code":"order_already_exists","message":"order already exists"}}')
            seen.append(client_order_id)
            answer = answers[len(self.sent) - 1]
            if isinstance(answer, Exception):
                raise answer
            return answer

        def portfolio_rows(self, path, key, min_ts=None, pages=1):
            return [{"fill_id": "f1", "market_ticker": "KXBTC15M-9", "count_fp": "2", "yes_price_dollars": "0.2100", "fee_cost": "0", "ts": 1.0, "book_side": "ask", "is_taker": False, "order_id": "o1", "action": "buy", "side": "no"}]

    spec = dataclasses.replace(lanes.lane_specs(s)["guarded"], buy_rest_seconds=20.0, buy_rest_below=0.01, price=(0.76, 0.92))
    shot = LongShot("KXBTC15M-9", "t", "", shot_ask - 0.01, shot_ask, 10.0, "2099-01-01T00:00:00Z", 0.1, lane="guarded", series="KXBTC15M", side="no")
    c = C()
    out = runner._place_resting(s, c, store, shot, "why", 2.0, spec, order_key, first_book)
    return c.sent, out, reads


def _cross():
    from kalshi_moonshot.kalshi import KalshiError

    return KalshiError('POST /portfolio/events/orders -> 400: {"error":{"code":"invalid_order","message":"invalid order","details":"post only cross"}}')


def _burst_exit(tmp_path, monkeypatch, *, with_holder, share):
    """A YES holder at an 85c bid; $455 bought AGAINST it (NO takers) in 3 s, and `with_holder` dollars bought WITH it (YES takers). Returns the sell signals queued."""
    w, _s, _store, close = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KXBTC15M-1", "guarded")]
    t.burst_bar, t.burst_favorite, t.burst_share = 400.0, 0.0, share
    t.cur_bid, t.cur_other_ask, t.side = 0.85, 0.16, "yes"
    now = close - 60
    sec = int(now)
    w._flow["KXBTC15M-1"] = {sec: [0.0, 6000.0, with_holder, 455.0]}  # [YES contracts, NO contracts, YES dollars, NO dollars]
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now, "count": 10, "taker": "no", "price": 0.84})
    return w._jobs.qsize()


def _spot_tape(tmp_path, rows, symbol="hl:xyz:GOLD"):
    from kalshi_moonshot import tape as tape_mod

    class S:
        tape_path = str(tmp_path / "tape.db")

    tape_mod.recent.clear()
    for ts, px in rows:
        tape_mod.recent.note_feed(symbol, ts, px)
    return S


def _shot(side):
    from kalshi_moonshot.strategy import LongShot

    return LongShot("KXGOLD15M-1", "", "", 0.74, 0.76, 0.0, "", 0.1, side=side, series="KXGOLD15M")


def _return_then_second_stay(w, close):
    now = close - 300
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.85, "ask": 0.86, "ts": now - 0.2})
    w.on_event({"kind": "index", "index_id": "BRTI", "ts": now, "value": 99.996})  # 0.004% through starts the watch
    w.on_event({"kind": "index", "index_id": "BRTI", "ts": now + 20, "value": 100.01})  # back on the winning side
    again = now + 60
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.84, "ask": 0.85, "ts": again - 0.2})
    w.on_event({"kind": "index", "index_id": "BRTI", "ts": again, "value": 99.996})  # a second cross
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.80, "ask": 0.81, "ts": again + watcher.THROUGH_STAY - 0.2})
    w.on_event({"kind": "index", "index_id": "BRTI", "ts": again + watcher.THROUGH_STAY, "value": 99.996})


def test_the_typical_minute_is_read_from_the_ticks_on_a_one_second_grid(monkeypatch):
    import math

    from kalshi_moonshot import ticks, variants

    now = 1_900_000_000.0
    vals = [100.0 * math.exp(1e-4 * (1 if i % 2 else -1)) for i in range(1000)]  # each second steps 2 bp
    ts = [now - len(vals) + 1 + i for i in range(len(vals))]
    monkeypatch.setattr(ticks.history, "window", lambda idx, a, b: (ts, vals))
    got = variants.unit_index("X", now)
    assert got is not None and abs(got - 2e-4 * math.sqrt(60.0)) < 1e-5
    monkeypatch.setattr(ticks.history, "window", lambda idx, a, b: (ts[-100:], vals[-100:]))
    assert variants.unit_index("X", now) is None  # under 300 s of history
    monkeypatch.setattr(ticks.history, "window", lambda idx, a, b: None)
    assert variants.unit_index("X", now) is None
