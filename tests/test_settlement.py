"""The last minute of a 15-minute crypto market: the settlement average, the guard, and the Coinbase tick parser."""
import json
import math

import pytest

from kalshi_moonshot import coinbasefeed, config, settlement, watcher
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


def test_the_coinbase_ticker_message_is_parsed_and_noise_is_ignored():
    msg = {"type": "ticker", "product_id": "BTC-USD", "price": "80580.12", "time": "2026-10-25T18:43:20.123456Z"}
    got = coinbasefeed.parse_ticker(msg)
    assert got is not None and got[0] == "BTC-USD" and got[2] == 80580.12 and got[1] == pytest.approx(1792953800.123456, abs=1.0)
    assert coinbasefeed.parse_ticker({"type": "subscriptions"}) is None
    assert coinbasefeed.parse_ticker({"type": "ticker", "product_id": "BTC-USD"}) is None  # no price
    assert coinbasefeed.parse_ticker({"type": "ticker", "product_id": "BTC-USD", "price": "x", "time": "bad"}) is None


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


def _watcher(tmp_path, monkeypatch, guard=0.10):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(f"[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nsettlement_guard={guard}\nsettlement_flip=true\nseries=['KXBTC15M']\nalways=true\n")
    s = config.load()
    store = Store(s.db_path)
    close = 2_000_000.0
    store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)", (1.0, "KXBTC15M-1", "bet", "observe", 0.90, "yes", "guarded", 3, 2.7, "2099-01-01T00:00:00Z"))
    store.db.commit()

    class Client:
        def market(self, ticker):
            return {"ticker": ticker, "floor_strike": 100.0, "close_time": "1970-01-24T03:33:20Z", "yes_bid_dollars": "0.90", "yes_ask_dollars": "0.91", "title": "t"}

    w = watcher.ProfitWatcher(lambda: s, Client(), lambda: Store(s.db_path))
    w.refresh()
    return w, s, store, close


def test_the_guard_arms_only_a_lane_that_asks_for_it(tmp_path, monkeypatch):
    w, *_ = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KXBTC15M-1", "guarded")]
    assert t.settle_guard == 0.10 and t.strike == 100.0 and t.close_ts == 2_000_000.0 and t.index_id == "BRTI"


def test_the_guard_sells_when_the_chance_falls_under_the_bid_and_stays_quiet_otherwise(tmp_path, monkeypatch):
    w, _s, _store, close = _watcher(tmp_path, monkeypatch)
    w.tape_factory = lambda: _Tape(close, fall_at=close - 70)  # the index falls from 70 s before the close
    t = w.targets[("KXBTC15M-1", "guarded")]
    now = close - 100  # 100 s left, spot still at 100.0: the chance is about even, the bid says 0.90
    w._spot["BRTI"] = (now, 100.0)
    assert w._settlement_hit(t, 0.90, now, {"bid": 0.90, "ask": 0.91}) is not None  # even odds against a 0.90 bid: cash out
    t.settle_checked = 0.0
    far = close - 400  # outside the 150 s window: no look at all
    w._spot["BRTI"] = (far, 100.0)
    assert w._settlement_hit(t, 0.90, far, {"bid": 0.90, "ask": 0.91}) is None
    t.settle_checked = 0.0
    w._spot["BRTI"] = (now, 120.0)  # the spot far above the strike: holding is worth the bid
    assert w._settlement_hit(t, 0.90, now, {"bid": 0.90, "ask": 0.91}) is None
    t.settle_checked = 0.0
    w._spot.clear()  # no index tick at all: say nothing
    assert w._settlement_hit(t, 0.90, now, {"bid": 0.90, "ask": 0.91}) is None


def test_the_guard_sale_goes_through_even_at_a_loss_and_logs_a_paper_flip(tmp_path, monkeypatch):
    w, _s, store, _close = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KXBTC15M-1", "guarded")]
    t.flip_hint = (0.12, 0.31)  # the other side costs 12c and the settlement chance says 31%
    w._inflight.add(("KXBTC15M-1", "guarded"))
    w.client.market = lambda ticker: {"ticker": ticker, "yes_bid_dollars": "0.60", "yes_ask_dollars": "0.62", "title": "t", "close_time": "2099-01-01T00:00:00Z"}
    w._sell("KXBTC15M-1", "guarded", "settlement-guard: test")  # the bid 0.60 is under the price paid 0.90: a profit-take would refuse; the guard must not
    assert [r for r in store.open_bets() if r["lane"] == "guarded"] == []  # (the logged paper flip is itself an open paper bet in its own lane)
    ex = store.db.execute("SELECT exit_price FROM decisions WHERE lane='guarded'").fetchone()
    assert ex["exit_price"] == pytest.approx(0.60)
    flip = store.db.execute("SELECT side, action, ask, model_p FROM decisions WHERE lane='settlement_flip'").fetchone()
    assert flip["side"] == "no" and flip["action"] == "bet" and flip["ask"] == pytest.approx(0.12)


def _quiet_minute(w, now, per_second=40):
    """Two minutes of ordinary trading before `now`: 40 contracts a second, half and half."""
    for k in range(125, 4, -1):
        w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now - k, "count": per_second / 2, "taker": "yes", "price": 0.9})
        w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now - k, "count": per_second / 2, "taker": "no", "price": 0.9})


def test_a_burst_is_volume_against_the_markets_own_normal_not_a_fixed_number(tmp_path, monkeypatch):
    w, _s, store, close = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KXBTC15M-1", "guarded")]
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.90, "ask": 0.91, "ts": now - 130})
    _quiet_minute(w, now)
    assert t.cur_bid == pytest.approx(0.90) and t.cur_other_ask == pytest.approx(0.10)
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now, "count": 100, "taker": "no", "price": 0.5})  # twice a normal second, but under the floor: nothing
    assert store.db.execute("SELECT COUNT(*) FROM decisions WHERE lane='burst_flip'").fetchone()[0] == 0
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 600, "taker": "no", "price": 0.5})  # 700 against in 2 s: about 6x the normal 3 s, nearly all of it against the YES holder
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 2, "count": 9000, "taker": "no", "price": 0.5})  # still one log
    rows = store.db.execute("SELECT side, ask, action, context FROM decisions WHERE lane='burst_flip'").fetchall()
    assert len(rows) == 1 and rows[0]["side"] == "no" and rows[0]["ask"] == pytest.approx(0.10)
    ctx = json.loads(rows[0]["context"])
    assert ctx["ratio"] >= 5 and ctx["share"] >= 0.6 and ctx["normal_second"] == pytest.approx(40.0)
    assert not w._jobs.empty() and w._jobs.get_nowait()[2].startswith("burst:")  # last 120 s: sell the favorite, that is the peak-crash exit


def test_a_burst_before_the_last_two_minutes_is_not_an_exit(tmp_path, monkeypatch):
    w, _s, store, close = _watcher(tmp_path, monkeypatch)
    now = close - 200  # 200 s left: holding still paid on the burst_flip record; the exit only fires in the last 120 s
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.90, "ask": 0.91, "ts": now - 130})
    _quiet_minute(w, now)
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 600, "taker": "no", "price": 0.5})
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 2, "count": 9000, "taker": "no", "price": 0.5})
    assert store.db.execute("SELECT COUNT(*) FROM decisions WHERE lane='burst_flip'").fetchone()[0] == 0
    assert w._jobs.empty()


def test_the_same_volume_in_a_busy_market_is_not_a_burst(tmp_path, monkeypatch):
    w, _s, store, close = _watcher(tmp_path, monkeypatch)
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.90, "ask": 0.91, "ts": now - 130})
    _quiet_minute(w, now, per_second=1500)  # a market that normally trades 1,500 contracts a second
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 3000, "taker": "no", "price": 0.5})  # the fixed bar of 3,000 would have fired; against this market it is ordinary
    assert store.db.execute("SELECT COUNT(*) FROM decisions WHERE lane='burst_flip'").fetchone()[0] == 0


def test_trading_with_the_holder_or_a_favorite_that_is_not_one_is_not_a_burst(tmp_path, monkeypatch):
    w, _s, store, close = _watcher(tmp_path, monkeypatch)
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.90, "ask": 0.91, "ts": now - 130})
    _quiet_minute(w, now)
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 5000, "taker": "yes", "price": 0.9})  # YES takers are with a YES holder
    assert store.db.execute("SELECT COUNT(*) FROM decisions WHERE lane='burst_flip'").fetchone()[0] == 0
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.40, "ask": 0.41, "ts": now + 5})  # no longer a favorite
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 6, "count": 20000, "taker": "no", "price": 0.5})
    assert store.db.execute("SELECT COUNT(*) FROM decisions WHERE lane='burst_flip'").fetchone()[0] == 0


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


def test_a_burst_on_a_live_lane_buys_the_other_side_for_real_and_records_it(tmp_path, monkeypatch):
    from kalshi_moonshot import runner

    w, _s, store, _close = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KXBTC15M-1", "guarded")]
    t.burst_buy, t.cur_other_ask = 1.0, 0.10
    bought = []

    def fake_place(settings, client, st, shot, why, count, **kw):
        bought.append((shot.side, shot.ask, count))
        return runner.Placed("bought NO 9/9 @ 0.1000", 9.0, 0.95)

    monkeypatch.setattr(runner, "place", fake_place)
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": True)
    w.client.authed = True
    w._burst_buy(t, "no", "burst: test", {"ratio": 9.0})
    assert bought == [("no", pytest.approx(0.12), 8.0)]  # a dollar at the ask plus the 2c allowance: 8 whole contracts
    row = store.db.execute("SELECT mode, side, contracts, stake_usd, lane FROM decisions WHERE lane='burst_flip'").fetchone()
    assert row["mode"] == "trade" and row["side"] == "no" and row["contracts"] == 9.0 and row["stake_usd"] == pytest.approx(0.95)


def test_a_burst_on_a_paper_lane_only_logs(tmp_path, monkeypatch):
    from kalshi_moonshot import runner

    w, _s, store, _close = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KXBTC15M-1", "guarded")]
    t.burst_buy, t.cur_other_ask = 1.0, 0.10
    monkeypatch.setattr(runner, "place", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no order on a paper lane")))
    w._burst_buy(t, "no", "burst: test", {})
    row = store.db.execute("SELECT mode FROM decisions WHERE lane='burst_flip'").fetchone()
    assert row["mode"] == "observe"


def test_dwell_counts_the_seconds_a_side_has_been_in_the_band(tmp_path):
    from kalshi_moonshot import db, runner
    from kalshi_moonshot.strategy import LongShot

    class S:
        tape_path = str(tmp_path / "tape.db")

    tape = db.connect(S.tape_path, "tape", shared=True)
    tape.execute("CREATE TABLE IF NOT EXISTS quotes (ts REAL NOT NULL, ticker TEXT NOT NULL, bid REAL, ask REAL, last REAL, sts INTEGER, bid_size REAL, ask_size REAL)")
    for ts, bid, ask in ((100.0, 0.60, 0.62), (110.0, 0.69, 0.71), (125.0, 0.72, 0.74), (140.0, 0.74, 0.76)):
        tape.execute("INSERT INTO quotes (ts, ticker, bid, ask) VALUES (?,?,?,?)", (ts, "KXBTC15M-1", bid, ask))
    tape.commit()
    yes = LongShot("KXBTC15M-1", "", "", 0.74, 0.76, 0.0, "", 0.1, side="yes")
    assert runner.dwell_seconds(S, yes, 0.70, now=145.0) == pytest.approx(35.0)  # under the band at t=100, in it from the quote at 110: 35 s
    assert runner.dwell_seconds(S, yes, 0.70, now=112.0) == pytest.approx(2.0)
    no = LongShot("KXBTC15M-1", "", "", 0.24, 0.26, 0.0, "", 0.1, side="no")  # the NO side costs 1 - bid: 0.40 .. 0.26: never in a 0.70 band
    assert runner.dwell_seconds(S, no, 0.70, now=145.0) == pytest.approx(0.0)


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


def test_a_lane_with_max_open_waits_when_it_is_full(tmp_path, monkeypatch):
    from kalshi_moonshot import runner
    from kalshi_moonshot.estimators import Estimate
    from kalshi_moonshot.strategy import LongShot

    w, s, store, _close = _watcher(tmp_path, monkeypatch)
    (tmp_path / "strategies.toml").write_text("[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nalways=true\nmax_open=2\nfixed_stake=2.0\nseries=['KXBTC15M']\n")
    s = config.load()

    class Fixed:
        def estimate(self, client, settings, shot):
            return Estimate(0.9, "fixed")

    shot = LongShot("KXBTC15M-9", "t", "", 0.88, 0.90, 10.0, "2099-01-01T00:00:00Z", 0.1, lane="guarded", series="KXBTC15M", side="yes")
    store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)", (1.0, "KXBTC15M-2", "bet", "observe", 0.90, "yes", "guarded", 3, 2.7, "2099-01-01T00:00:00Z"))
    store.db.commit()  # the fixture already holds KXBTC15M-1: two open, the lane's cap
    out = runner.evaluate(s, w.client, None, store, shot, estimators={"market": Fixed()})
    assert out.action == "pass" and "lane full" in out.note


def test_a_resting_buy_reports_what_filled_and_the_fee_charged(tmp_path, monkeypatch):
    import dataclasses

    from kalshi_moonshot import lanes, runner
    from kalshi_moonshot.strategy import LongShot

    _w, s, store, _close = _watcher(tmp_path, monkeypatch)
    monkeypatch.setattr(runner.time, "sleep", lambda x: None)

    class C:
        def __init__(self):
            self.sent = []

        def buy_resting(self, ticker, side, count, price, seconds, client_order_id=None, post_only=True):
            self.sent.append((side, count, price, seconds))
            return {"order_id": "o1"}

        def portfolio_rows(self, path, key, min_ts=None, pages=1):
            return [{"fill_id": "f1", "market_ticker": "KXBTC15M-9", "count_fp": "2", "yes_price_dollars": "0.1900", "no_price_dollars": "0.8100", "fee_cost": "0", "ts": 1.0, "book_side": "ask", "is_taker": False, "order_id": "o1", "action": "buy", "side": "no"}]

    spec = dataclasses.replace(lanes.lane_specs(s)["guarded"], buy_rest_seconds=20.0, buy_rest_below=0.01)
    shot = LongShot("KXBTC15M-9", "t", "", 0.18, 0.82, 10.0, "2099-01-01T00:00:00Z", 0.1, lane="guarded", series="KXBTC15M", side="no")
    c = C()
    out = runner._place_resting(s, c, store, shot, "why", 2.0, spec, None)
    assert c.sent == [("no", 2.0, pytest.approx(0.81), 20.0)]
    assert out.filled == 2.0 and out.cost == pytest.approx(1.62) and "fee $0.0000" in out.note
