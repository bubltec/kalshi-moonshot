"""The last minute of a 15-minute crypto market: the settlement average, the guard, and the Coinbase tick parser."""
import json
import math
import queue
import time
from datetime import UTC, datetime

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


def _watcher(tmp_path, monkeypatch, guard=0.10, ticker="KXBTC15M-1"):
    series = ticker.split("-")[0]
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(f"[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nsettlement_guard={guard}\nsettlement_flip=true\nseries=['{series}']\nalways=true\n")
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


def _quiet_minute(w, now, per_second=40, ticker="KXBTC15M-1"):
    """Two minutes of ordinary trading before `now`: 40 contracts a second, half and half."""
    for k in range(125, 4, -1):
        w.on_event({"kind": "trade", "ticker": ticker, "ts": now - k, "count": per_second / 2, "taker": "yes", "price": 0.9})
        w.on_event({"kind": "trade", "ticker": ticker, "ts": now - k, "count": per_second / 2, "taker": "no", "price": 0.9})


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
    assert ctx["ratio"] >= 5 and ctx["normal_second"] == pytest.approx(20.0)  # this SIDE's own median second (half of the mixed 40)
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


def test_a_silent_book_cannot_five_times_its_own_silence(tmp_path, monkeypatch):
    w, _s, store, close = _watcher(tmp_path, monkeypatch)
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.90, "ask": 0.91, "ts": now - 130})
    _quiet_minute(w, now, per_second=0)  # no trades: the median second is 0, so 5x would be 5x of nothing
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 600, "taker": "no", "price": 0.5})
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 2, "count": 9000, "taker": "no", "price": 0.5})
    assert store.db.execute("SELECT COUNT(*) FROM decisions WHERE lane='burst_flip'").fetchone()[0] == 0
    assert w._jobs.empty()


def test_a_300_contract_print_on_a_thin_gold_book_is_not_a_crash(tmp_path, monkeypatch):
    w, _s, store, close = _watcher(tmp_path, monkeypatch, ticker="KXGOLD15M-1")
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXGOLD15M-1", "bid": 0.90, "ask": 0.91, "ts": now - 130})
    _quiet_minute(w, now, per_second=2, ticker="KXGOLD15M-1")  # copper/natgas-like: a handful of contracts a second
    w.on_event({"kind": "trade", "ticker": "KXGOLD15M-1", "ts": now + 1, "count": 324, "taker": "no", "price": 0.5})  # today's copper "358x" print
    assert store.db.execute("SELECT COUNT(*) FROM decisions WHERE lane='burst_flip'").fetchone()[0] == 0
    assert w._jobs.empty()


def test_a_burst_on_gold_with_a_real_baseline_sells(tmp_path, monkeypatch):
    w, _s, store, close = _watcher(tmp_path, monkeypatch, ticker="KXGOLD15M-1")
    t = w.targets[("KXGOLD15M-1", "guarded")]
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXGOLD15M-1", "bid": 0.90, "ask": 0.91, "ts": now - 130})
    assert t.cur_bid == pytest.approx(0.90)
    _quiet_minute(w, now, ticker="KXGOLD15M-1")
    w.on_event({"kind": "trade", "ticker": "KXGOLD15M-1", "ts": now + 1, "count": 600, "taker": "no", "price": 0.5})
    w.on_event({"kind": "trade", "ticker": "KXGOLD15M-1", "ts": now + 2, "count": 9000, "taker": "no", "price": 0.5})
    rows = store.db.execute("SELECT side, ask FROM decisions WHERE lane='burst_flip'").fetchall()
    assert len(rows) == 1 and rows[0]["side"] == "no"
    assert not w._jobs.empty() and w._jobs.get_nowait()[2].startswith("burst:")


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


def test_a_barely_5x_print_under_500_contracts_is_not_a_moonshot(tmp_path, monkeypatch):
    w, _s, store, close = _hunt_watcher(tmp_path, monkeypatch)
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.92, "ask": 0.93, "ts": now - 130})
    w.refresh()
    _quiet_minute(w, now)  # 20 NO contracts a second; 350 in one second is about 5.7x and under the 500 cushion
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 350, "taker": "no", "price": 0.08})
    assert store.db.execute("SELECT COUNT(*) FROM decisions WHERE lane='crypto_moonshot'").fetchone()[0] == 0


def test_a_5x_print_of_500_contracts_is_still_a_moonshot(tmp_path, monkeypatch):
    w, _s, store, close = _hunt_watcher(tmp_path, monkeypatch)
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.92, "ask": 0.93, "ts": now - 130})
    w.refresh()
    _quiet_minute(w, now)
    _sat_cheap(w, now + 1, bid=0.92, ask=0.93)  # NO has been 8c for 20s, and the quiet minute already bought it
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 520, "taker": "no", "price": 0.08})
    row = store.db.execute("SELECT side FROM decisions WHERE lane='crypto_moonshot'").fetchone()
    assert row is not None and row["side"] == "no"


def test_a_hunt_does_not_buy_a_side_that_is_crashing(tmp_path, monkeypatch):
    w, _s, store, close = _hunt_watcher(tmp_path, monkeypatch)
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.90, "ask": 0.91, "ts": now - 130})
    w.refresh()
    _quiet_minute(w, now)
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.35, "ask": 0.36, "ts": now - 8})  # NO costs 65c
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.85, "ask": 0.86, "ts": now + 1})  # NO costs 15c: down 50c
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 600, "taker": "no", "price": 0.15})
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 2, "count": 9000, "taker": "no", "price": 0.15})
    assert store.db.execute("SELECT COUNT(*) FROM decisions WHERE lane='crypto_moonshot'").fetchone()[0] == 0


def test_a_few_cent_dip_is_not_a_crash(tmp_path, monkeypatch):
    w, _s, store, close = _hunt_watcher(tmp_path, monkeypatch)
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.90, "ask": 0.91, "ts": now - 130})
    w.refresh()
    _quiet_minute(w, now)
    _sat_cheap(w, now, bid=0.91, ask=0.92)  # NO sat at 9c, then eased to 7c
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.93, "ask": 0.94, "ts": now + 1})
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 600, "taker": "no", "price": 0.10})
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 2, "count": 9000, "taker": "no", "price": 0.10})
    row = store.db.execute("SELECT side FROM decisions WHERE lane='crypto_moonshot'").fetchone()
    assert row is not None and row["side"] == "no"


def test_a_burst_that_just_arrived_at_a_low_price_is_not_a_moonshot(tmp_path, monkeypatch):
    w, _s, store, close = _hunt_watcher(tmp_path, monkeypatch)
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.90, "ask": 0.91, "ts": now - 130})
    w.refresh()
    _quiet_minute(w, now)
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.60, "ask": 0.61, "ts": now - 14})  # NO costs 40c
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.90, "ask": 0.91, "ts": now + 1})  # NO costs 10c now
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 600, "taker": "no", "price": 0.10})
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 2, "count": 9000, "taker": "no", "price": 0.10})
    assert store.db.execute("SELECT COUNT(*) FROM decisions WHERE lane='crypto_moonshot'").fetchone()[0] == 0


def test_one_cheap_quote_is_not_repeat_buying(tmp_path, monkeypatch):
    w, _s, store, close = _hunt_watcher(tmp_path, monkeypatch)
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.90, "ask": 0.91, "ts": now - 130})
    w.refresh()
    _quiet_minute(w, now)
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.90, "ask": 0.91, "ts": now + 1})
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 600, "taker": "no", "price": 0.10})
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 2, "count": 9000, "taker": "no", "price": 0.10})
    assert store.db.execute("SELECT COUNT(*) FROM decisions WHERE lane='crypto_moonshot'").fetchone()[0] == 0


def test_a_burst_hunt_buys_the_cheap_side_without_a_held_favorite(tmp_path, monkeypatch):
    w, _s, store, close = _hunt_watcher(tmp_path, monkeypatch)
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.90, "ask": 0.91, "ts": now - 130})
    w.refresh()  # close time from the market
    _quiet_minute(w, now)
    _sat_cheap(w, now + 1)
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 600, "taker": "no", "price": 0.5})
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 2, "count": 9000, "taker": "no", "price": 0.5})
    row = store.db.execute("SELECT side, ask, lane, mode FROM decisions WHERE lane='crypto_moonshot'").fetchone()
    assert row is not None and row["side"] == "no" and row["ask"] == pytest.approx(0.10) and row["mode"] == "observe"


def test_a_one_cent_ask_is_not_a_moonshot(tmp_path, monkeypatch):
    w, _s, store, close = _hunt_watcher(tmp_path, monkeypatch)
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.99, "ask": 1.00, "ts": now - 130})
    w.refresh()
    _quiet_minute(w, now)
    _sat_cheap(w, now + 1, bid=0.99, ask=1.00)  # NO has been 1c
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 600, "taker": "no", "price": 0.01})
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 2, "count": 9000, "taker": "no", "price": 0.01})
    assert store.db.execute("SELECT COUNT(*) FROM decisions WHERE lane='crypto_moonshot'").fetchone()[0] == 0


def test_a_burst_hunt_ignores_a_side_outside_2_to_20_cents(tmp_path, monkeypatch):
    w, _s, store, close = _hunt_watcher(tmp_path, monkeypatch)
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.70, "ask": 0.72, "ts": now - 130})  # NO costs 30c
    w.refresh()
    _quiet_minute(w, now)
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 600, "taker": "no", "price": 0.5})
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 2, "count": 9000, "taker": "no", "price": 0.5})
    assert store.db.execute("SELECT COUNT(*) FROM decisions WHERE lane='crypto_moonshot'").fetchone()[0] == 0


def test_a_favorite_pile_on_does_not_hide_a_cheap_side_burst(tmp_path, monkeypatch):
    """YES at 95c prints a huge volume; NO at 5c also 5xs its own tape. The burst is the 5c buys."""
    w, _s, store, close = _hunt_watcher(tmp_path, monkeypatch)
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.95, "ask": 0.96, "ts": now - 130})
    w.refresh()
    _quiet_minute(w, now)
    _sat_cheap(w, now + 1, bid=0.95, ask=0.96)  # NO has been 5c
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 8000, "taker": "yes", "price": 0.95})
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 800, "taker": "no", "price": 0.05})
    row = store.db.execute("SELECT side, ask FROM decisions WHERE lane='crypto_moonshot'").fetchone()
    assert row is not None and row["side"] == "no" and row["ask"] == pytest.approx(0.05)


def test_a_favorite_only_spike_is_not_a_cheap_burst(tmp_path, monkeypatch):
    w, _s, store, close = _hunt_watcher(tmp_path, monkeypatch)
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.95, "ask": 0.96, "ts": now - 130})
    w.refresh()
    _quiet_minute(w, now)
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 9000, "taker": "yes", "price": 0.95})
    assert store.db.execute("SELECT COUNT(*) FROM decisions WHERE lane='crypto_moonshot'").fetchone()[0] == 0


def test_a_burst_hunt_ignores_gold(tmp_path, monkeypatch):
    w, _s, store, close = _hunt_watcher(tmp_path, monkeypatch)  # lane series is KXBTC15M only
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXGOLD15M-1", "bid": 0.90, "ask": 0.91, "ts": now - 130})
    w.refresh()
    _quiet_minute(w, now, ticker="KXGOLD15M-1")
    w.on_event({"kind": "trade", "ticker": "KXGOLD15M-1", "ts": now + 1, "count": 600, "taker": "no", "price": 0.5})
    w.on_event({"kind": "trade", "ticker": "KXGOLD15M-1", "ts": now + 2, "count": 9000, "taker": "no", "price": 0.5})
    assert store.db.execute("SELECT COUNT(*) FROM decisions WHERE lane='crypto_moonshot'").fetchone()[0] == 0


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


def test_a_sale_that_fills_nothing_is_retried_lower_against_a_fresh_book(tmp_path, monkeypatch):
    from kalshi_moonshot import runner

    monkeypatch.setattr(runner.time, "sleep", lambda x: None)
    _w, s, store, _close = _watcher(tmp_path, monkeypatch)
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": True)
    store.add_order(ticker="KXBTC15M-1", env=s.env, count=2.0, price=0.90, fill_count=2.0, cost_usd=1.8, fee_usd=0.0, order_id="b", contract="yes")
    row = next(r for r in store.open_bets() if r["ticker"] == "KXBTC15M-1")

    class C:
        def __init__(self):
            self.sells = []

        def sell_yes_ioc(self, ticker, count, price):
            self.sells.append((round(count, 2), price))
            if len(self.sells) < 3:
                return {"fill_count": "0", "order_id": f"s{len(self.sells)}"}  # the first two find no bid at their price
            return {"fill_count": f"{count}", "average_fill_price": "0.70", "average_fee_paid": "0", "order_id": "s3"}

        def orderbook(self, ticker):
            return {"orderbook_fp": {"yes_dollars": [["0.55", "10"], ["0.72", "4"]], "no_dollars": []}}

    c = C()
    line = runner.exit_position(s, c, store, row, 0.80, "burst: test", slip=0.02)
    assert line.startswith("sold 2")
    assert c.sells[0] == (2.0, 0.78) and c.sells[1] == (2.0, 0.70) and c.sells[2][1] == 0.67  # 0.72 - 0.02, then 0.72 - 0.05
    assert store.net_contracts("KXBTC15M-1", s.env) < 0.005


def test_best_bid_reads_the_side_of_the_book_it_sells_into():
    from kalshi_moonshot.strategy import best_bid

    book = {"orderbook_fp": {"yes_dollars": [["0.40", "5"], ["0.61", "2"], ["0.70", "0"]], "no_dollars": [["0.30", "8"]]}}
    assert best_bid(book, "yes") == 0.61  # a zero-size row is not a bid
    assert best_bid(book, "no") == 0.30
    assert best_bid({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}, "yes") is None


def test_a_lane_can_keep_its_stop_quiet_until_the_last_minutes(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.guarded]\nestimator='market'\nprice=[0.5,0.99]\nhours=[0.0,1.0]\nsettlement_guard=0.0\nsettlement_flip=false\nseries=['KXBTC15M']\nalways=true\nstop_loss=true\nstop_loss_at=0.45\nstop_from_seconds=180\n")
    s = config.load()
    store = Store(s.db_path)
    close = time.time() + 600.0
    iso = datetime.fromtimestamp(close, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)", (1.0, "KXBTC15M-1", "bet", "observe", 0.80, "yes", "guarded", 3, 2.4, iso))
    store.db.commit()

    class Client:
        def market(self, ticker):
            return {"ticker": ticker}

    w = watcher.ProfitWatcher(lambda: s, Client(), lambda: Store(s.db_path))
    w.refresh()
    t = w.targets[("KXBTC15M-1", "guarded")]
    assert t.stop_from == pytest.approx(close - 180.0, abs=2.0)
    assert w._stop_hit(t, 0.30, time.time()) is None  # 10 minutes out the dip is ignored
    assert w._stop_hit(t, 0.30, close - 170.0) is not None  # inside the last three minutes it sells (the last two have their own rule: a hover or spike is not sold, and this test has no price history to call a trend)


def test_a_resting_buy_is_sized_to_its_own_price_and_spends_the_whole_stake(tmp_path, monkeypatch):
    from kalshi_moonshot import lanes, sizing

    _w, s, _store, _close = _watcher(tmp_path, monkeypatch)
    spec = lanes.lane_specs(s)["guarded"]
    spec = __import__("dataclasses").replace(spec, fixed_stake=2.0, max_bet_pct=1.0, max_exposure_pct=1.0)
    at_ask = sizing.size_bet(spec, s, 0.9, 0.90, 100.0, 100.0, 0.0, fractional=True)
    resting = sizing.size_bet(spec, s, 0.9, 0.90, 100.0, 100.0, 0.0, fractional=True, maker_price=0.89)
    assert at_ask.contracts == pytest.approx(2.20) and at_ask.cost_usd == pytest.approx(2.0, abs=0.05)  # the ask plus the taker fee
    assert resting.contracts == pytest.approx(2.24) and resting.cost_usd == pytest.approx(1.9936)  # 2.24 contracts at the resting 0.89, no fee


def test_a_hunt_lane_may_widen_how_expensive_a_parked_side_can_be():
    from collections import deque

    now = 1000.0
    quotes = deque((now - 20.0 + k, 0.84, 0.86) for k in range(21))  # the NO side's ask sits at 0.16 for 20 s: YES bid 0.84
    # side "no": ask = 1 - yes_bid = 0.16
    assert watcher._hunt_parked(quotes, "no", now, 0.16) is False  # over the default 10c cap
    assert watcher._hunt_parked(quotes, "no", now, 0.16, 0.17) is True  # a lane that buys up to 17c accepts it
    quotes2 = deque((now - 20.0 + k, 0.90, 0.92) for k in range(21))  # 0.10: parked under the default cap
    assert watcher._hunt_parked(quotes2, "no", now, 0.10) is True


def test_a_hunt_lane_that_requires_a_prior_win_buys_only_series_that_have_won(tmp_path, monkeypatch):
    import dataclasses

    from kalshi_moonshot import lanes, runner

    w, s, store, _close = _watcher(tmp_path, monkeypatch)
    spec = dataclasses.replace(lanes.lane_specs(s)["guarded"], name="guarded", require_prior_win=True, fixed_stake=0.5, max_open=0)
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": True)
    w.client.authed = True
    placed = []
    monkeypatch.setattr(runner, "place", lambda *a, **k: (placed.append(a[3].ticker) or runner.Placed("bought", 5.0, 0.4)))
    h = watcher.Hunt("KXETH15M-1", "guarded", close_ts=time.time() + 100)
    store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd) VALUES (?,?,?,?,?,?,?,?,?)", (1.0, "KXETH15M-0", "bet", "observe", 0.05, "no", "guarded", 10, 0.5))
    store.db.execute("INSERT INTO windows (ticker, series, close_ts, result, fetched_ts) VALUES (?,?,?,?,?)", ("KXETH15M-0", "KXETH15M", 5.0, "no", 1.0))
    store.db.commit()
    w._hunt_buy(h, spec, "no", 0.05, "burst hunt: test", {})  # ETH has won for this lane: real
    h2 = watcher.Hunt("KXBTC15M-1", "guarded", close_ts=time.time() + 100)
    w._hunt_buy(h2, spec, "no", 0.05, "burst hunt: test", {})  # BTC has not: paper
    assert placed == ["KXETH15M-1"]
    rows = {r["ticker"]: r["mode"] for r in store.db.execute("SELECT ticker, mode FROM decisions WHERE ticker LIKE '%-1' AND lane='guarded'").fetchall()}
    assert rows["KXETH15M-1"] == "trade" and rows["KXBTC15M-1"] == "observe"


def test_a_side_sliding_to_a_cheaper_price_is_not_a_dollar_burst():
    sec = 1000
    book = {}
    for k in range(4, 125):  # a quiet side: 10 contracts a second at 30c = $3 a second
        book[sec - k] = [10.0, 0.0, 3.0, 0.0]
    for k in range(3):  # the price has fallen to 3c: the same $3 a second buys 100 contracts, ten times the count
        book[sec - k] = [100.0, 0.0, 3.0, 0.0]
    contracts = watcher._side_burst(book, sec, 0)
    assert contracts[2] > 5.0  # in contracts it looks like a 10x burst
    assert watcher._side_dollar_ratio(book, sec, 0) < 2.0  # in dollars the money behind it is the same as before
    for k in range(3):  # real money: $30 a second
        book[sec - k] = [1000.0, 0.0, 30.0, 0.0]
    assert watcher._side_dollar_ratio(book, sec, 0) >= 5.0


def test_the_flip_trigger_buys_the_cheap_side_on_dollars_against_a_90c_favorite(tmp_path, monkeypatch):
    import dataclasses

    from kalshi_moonshot import lanes

    w, s, _store, _close = _watcher(tmp_path, monkeypatch)
    _buy_inline(w)
    spec = dataclasses.replace(lanes.lane_specs(s)["guarded"], name="guarded", price=(0.02, 0.15), hunt_dollars=400.0, hunt_dollars_btc=800.0, hunt_favorite=0.90, sides=("yes", "no"), fixed_stake=0.5, max_open=0)
    bought = []
    monkeypatch.setattr(w, "_hunt_buy", lambda h, sp, side, ask, reason, ctx: bought.append((h.ticker, side, round(ask, 2), ctx["bar"])))
    now = 1_000_000.0
    sec = int(now)
    h = watcher.Hunt("KXETH15M-1", "guarded", close_ts=now + 60.0, yes_bid=0.93, yes_ask=0.94)  # YES is the 93c favorite; the NO side costs 0.07
    h.quotes.extend((now - 20.0 + i, 0.93, 0.94) for i in range(21))  # NO has been sitting at 7c, not falling into it
    book = {sec: [0.0, 300.0, 0.0, 21.0], sec - 1: [0.0, 200.0, 0.0, 14.0]}  # NO takers bought $35 in 3 s: well under the bar
    w._on_hunt(h, spec, now, book, sec)
    assert bought == []
    book[sec - 2] = [0.0, 6000.0, 0.0, 420.0]  # $455 against the favorite
    w._on_hunt(h, spec, now, book, sec)
    assert bought == [("KXETH15M-1", "no", 0.07, 400.0)]
    bought.clear()
    hb = watcher.Hunt("KXBTC15M-1", "guarded", close_ts=now + 60.0, yes_bid=0.93, yes_ask=0.94)  # BTC needs $800: $455 is not enough
    hb.quotes.extend((now - 20.0 + i, 0.93, 0.94) for i in range(21))
    w._on_hunt(hb, spec, now, book, sec)
    assert bought == []
    book[sec - 2] = [0.0, 12000.0, 0.0, 820.0]
    w._on_hunt(hb, spec, now, book, sec)
    assert bought and bought[0][3] == 800.0
    hf = watcher.Hunt("KXETH15M-2", "guarded", close_ts=now + 60.0, yes_bid=0.87, yes_ask=0.88)  # NO is 13c, in the band, but the favorite is 87c: this lane wants 90c
    hf.quotes.extend((now - 20.0 + i, 0.87, 0.88) for i in range(21))
    bought.clear()
    w._on_hunt(hf, spec, now, book, sec)
    assert bought == []
    fell = watcher.Hunt("KXETH15M-3", "guarded", close_ts=now + 60.0, yes_bid=0.90, yes_ask=0.91)  # NO now 10c, but it was 40c a few seconds ago
    fell.quotes.extend((now - 20.0 + i, 0.60, 0.62) for i in range(12))
    fell.quotes.append((now, 0.90, 0.91))
    bought.clear()
    w._on_hunt(fell, spec, now, book, sec)
    assert bought == []


def test_the_burst_exit_can_use_the_moonshot_dollars_instead_of_a_multiple(tmp_path, monkeypatch):
    w, _s, _store, close = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KXBTC15M-1", "guarded")]
    t.burst_bar = 400.0
    t.cur_bid, t.cur_other_ask, t.side = 0.93, 0.08, "yes"
    now = close - 60
    sec = int(now)
    book = {sec: [0.0, 5000.0, 0.0, 250.0]}  # NO takers (against the YES holder) bought $250 in the last 3 s: under the $400 bar, however many contracts it was
    w._flow["KXBTC15M-1"] = book
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now, "count": 10, "taker": "no", "price": 0.92})
    assert w._jobs.empty()
    book[sec - 1] = [0.0, 3000.0, 0.0, 200.0]  # $450+ in all
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now, "count": 10, "taker": "no", "price": 0.92})
    job = w._jobs.get_nowait()
    assert job[2].startswith("burst: $4")


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


def test_the_socket_only_queues_a_hunt_buy_and_the_worker_places_it(tmp_path, monkeypatch):
    from kalshi_moonshot import runner

    w, store, spec, h = _queued_hunt(tmp_path, monkeypatch)
    placed = []
    monkeypatch.setattr(runner, "place", lambda *a, **k: (placed.append(a[3].ticker) or runner.Placed("bought", 5.0, 0.4)))
    w._buys.put(watcher.BuySignal("hunt", "no", 0.05, "burst hunt: test", {}, hunt=h, spec=spec))
    assert placed == [] and store.db.execute("SELECT COUNT(*) FROM decisions WHERE ticker='KXETH15M-1'").fetchone()[0] == 0  # nothing happens until the worker takes it
    w._drain_buys()
    assert placed == ["KXETH15M-1"]
    assert store.db.execute("SELECT mode FROM decisions WHERE ticker='KXETH15M-1'").fetchone()["mode"] == "trade"


def test_a_live_hunt_order_that_does_not_fill_says_why_it_missed(tmp_path, monkeypatch):
    from kalshi_moonshot import runner

    w, store, spec, h = _queued_hunt(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "place", lambda *a, **k: runner.Placed("order failed: no fill at the limit"))
    w._buys.put(watcher.BuySignal("hunt", "no", 0.05, "burst hunt: test", {}, hunt=h, spec=spec))
    w._drain_buys()
    row = store.db.execute("SELECT mode, reason, context FROM decisions WHERE ticker='KXETH15M-1'").fetchone()
    assert row["mode"] == "observe" and "live order not filled: order failed: no fill at the limit" in row["reason"]
    ctx = json.loads(row["context"])
    assert ctx["live"] is True and ctx["miss"] == "order failed: no fill at the limit"


def test_a_rate_limited_hunt_order_is_logged_not_lost(tmp_path, monkeypatch):
    from kalshi_moonshot import runner
    from kalshi_moonshot.kalshi import KalshiRateLimited

    w, store, spec, h = _queued_hunt(tmp_path, monkeypatch)

    def limited(*a, **k):
        raise KalshiRateLimited("429")

    monkeypatch.setattr(runner, "place", limited)
    w._buys.put(watcher.BuySignal("hunt", "no", 0.05, "burst hunt: test", {}, hunt=h, spec=spec))
    w._drain_buys()
    row = store.db.execute("SELECT mode, reason FROM decisions WHERE ticker='KXETH15M-1'").fetchone()
    assert row is not None and row["mode"] == "observe" and "rate limited" in row["reason"]


def test_a_hunt_buy_that_waited_too_long_in_the_queue_is_dropped(tmp_path, monkeypatch):
    from kalshi_moonshot import runner

    w, store, spec, h = _queued_hunt(tmp_path, monkeypatch)
    placed = []
    monkeypatch.setattr(runner, "place", lambda *a, **k: placed.append(1))
    w._buys.put(watcher.BuySignal("hunt", "no", 0.05, "burst hunt: test", {}, hunt=h, spec=spec, queued=time.time() - spec.hunt_stale_seconds - 1.0))
    w._drain_buys()
    assert placed == [] and store.db.execute("SELECT COUNT(*) FROM decisions WHERE ticker='KXETH15M-1'").fetchone()[0] == 0


def test_a_burst_on_a_held_favorite_queues_the_flip_buy_and_the_worker_places_it(tmp_path, monkeypatch):
    from kalshi_moonshot import runner

    w, _s, store, close = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KXBTC15M-1", "guarded")]
    t.burst_buy = 1.0
    bought = []
    monkeypatch.setattr(runner, "place", lambda *a, **k: (bought.append(a[3].side) or runner.Placed("bought NO 8/8 @ 0.1000", 8.0, 0.85)))
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": True)
    w.client.authed = True
    now = close - 120
    w.on_event({"kind": "quote", "ticker": "KXBTC15M-1", "bid": 0.90, "ask": 0.91, "ts": now - 130})
    _quiet_minute(w, now)
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 1, "count": 600, "taker": "no", "price": 0.5})
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now + 2, "count": 9000, "taker": "no", "price": 0.5})
    sell = w._jobs.get_nowait()
    assert isinstance(sell, watcher.SellSignal) and sell.why.startswith("burst:")  # the favorite is sold by the sell worker
    assert w._buys.qsize() == 1 and bought == []  # and the other side is only queued: the socket bought nothing
    w._drain_buys()
    assert bought == ["no"]
    row = store.db.execute("SELECT mode, side FROM decisions WHERE lane='burst_flip'").fetchone()
    assert row["mode"] == "trade" and row["side"] == "no"


def test_a_hunt_lane_at_its_max_open_buys_nothing_more(tmp_path, monkeypatch, caplog):
    import dataclasses

    from kalshi_moonshot import runner

    w, store, spec, h = _queued_hunt(tmp_path, monkeypatch)  # the fixture already holds one open bet in this lane
    monkeypatch.setattr(runner, "place", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no order past max_open")))
    with caplog.at_level("INFO", logger="moonshot"):
        w._hunt_buy(h, dataclasses.replace(spec, max_open=1), "no", 0.05, "burst hunt: test", {})
    assert store.db.execute("SELECT COUNT(*) FROM decisions WHERE ticker='KXETH15M-1'").fetchone()[0] == 0
    assert "already holds 1 of its 1 open bets" in caplog.text  # a skipped trigger is not silent
    monkeypatch.setattr(runner, "place", lambda *a, **k: runner.Placed("bought", 5.0, 0.4))
    w._hunt_buy(h, dataclasses.replace(spec, max_open=2), "no", 0.05, "burst hunt: test", {})  # one slot left
    assert store.db.execute("SELECT COUNT(*) FROM decisions WHERE ticker='KXETH15M-1'").fetchone()[0] == 1


def test_a_hunt_does_not_buy_a_market_the_lane_already_holds(tmp_path, monkeypatch):
    from kalshi_moonshot import runner

    w, store, spec, _h = _queued_hunt(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "place", lambda *a, **k: (_ for _ in ()).throw(AssertionError("already held")))
    held = watcher.Hunt("KXBTC15M-1", "guarded", close_ts=time.time() + 100)  # the fixture's open bet
    before = store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    w._hunt_buy(held, spec, "no", 0.05, "burst hunt: test", {})
    assert store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == before


def test_a_live_hunt_order_is_sized_to_the_stake_with_the_two_cent_allowance(tmp_path, monkeypatch):
    from kalshi_moonshot import runner

    w, _store, spec, h = _queued_hunt(tmp_path, monkeypatch)
    seen = []

    def fake_place(settings, client, st, shot, why, count, **kw):
        seen.append((shot.lane, shot.side, shot.bid, shot.ask, count, shot.series))
        return runner.Placed("bought", count, 0.4)

    monkeypatch.setattr(runner, "place", fake_place)
    w._hunt_buy(h, spec, "no", 0.05, "burst hunt: test", {})  # fixed_stake 0.5
    assert seen == [("guarded", "no", pytest.approx(0.04), pytest.approx(0.07), 7.0, "KXETH15M")]  # int(0.5 / 0.07) whole contracts, limit 2c over the ask


def test_a_paper_hunt_is_sized_by_the_stake_at_the_ask_and_defaults_to_two_dollars(tmp_path, monkeypatch):
    import dataclasses

    from kalshi_moonshot import runner

    w, store, spec, h = _queued_hunt(tmp_path, monkeypatch)
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": False)
    monkeypatch.setattr(runner, "place", lambda *a, **k: (_ for _ in ()).throw(AssertionError("paper lane")))
    w._hunt_buy(h, dataclasses.replace(spec, fixed_stake=0.0), "no", 0.05, "burst hunt: test", {})
    row = store.db.execute("SELECT mode, contracts, stake_usd, ask, lane, close_time FROM decisions WHERE ticker='KXETH15M-1'").fetchone()
    assert row["mode"] == "observe" and row["lane"] == "guarded" and row["ask"] == pytest.approx(0.05)
    assert row["contracts"] == pytest.approx(28.0) and row["stake_usd"] == pytest.approx(1.4)  # no fixed stake: $2 over the 7c limit is 28 whole contracts, the same count a live order asks for, paid at the ask
    assert len(row["close_time"]) == 20 and row["close_time"].endswith("Z")


def test_a_flip_buy_is_live_by_the_holding_lane_and_logged_as_burst_flip(tmp_path, monkeypatch):
    from kalshi_moonshot import runner

    w, _s, store, _close = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KXBTC15M-1", "guarded")]
    t.burst_buy, t.cur_other_ask = 1.0, 0.10
    asked, seen = [], []
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": (asked.append((lane, series)) or True))
    monkeypatch.setattr(runner, "place", lambda settings, client, st, shot, why, count, **kw: (seen.append((shot.lane, shot.side, count)) or runner.Placed("bought", count, 0.9)))
    w.client.authed = True
    w._burst_buy(t, "no", "burst: test", {})
    assert asked == [("guarded", "KXBTC15M")] and seen == [("burst_flip", "no", 8.0)]
    row = store.db.execute("SELECT lane, close_time FROM decisions WHERE lane='burst_flip'").fetchone()
    assert row["close_time"] == ""


def test_a_paper_flip_buy_is_one_contract_at_the_ask(tmp_path, monkeypatch):
    w, _s, store, _close = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KXBTC15M-1", "guarded")]
    t.burst_buy, t.cur_other_ask = 1.0, 0.10
    w._burst_buy(t, "no", "burst: test", {})
    row = store.db.execute("SELECT mode, contracts, stake_usd, reason FROM decisions WHERE lane='burst_flip'").fetchone()
    assert row["mode"] == "observe" and row["contracts"] == 1.0 and row["stake_usd"] == pytest.approx(0.10) and row["reason"] == "burst: test"


def test_a_flip_order_that_does_not_fill_says_why_it_missed(tmp_path, monkeypatch):
    from kalshi_moonshot import runner
    from kalshi_moonshot.kalshi import KalshiRateLimited

    w, _s, store, _close = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KXBTC15M-1", "guarded")]
    t.burst_buy, t.cur_other_ask = 1.0, 0.10
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": True)
    w.client.authed = True
    monkeypatch.setattr(runner, "place", lambda *a, **k: runner.Placed("order failed: no fill at the limit"))
    w._burst_buy(t, "no", "burst: test", {})

    def limited(*a, **k):
        raise KalshiRateLimited("429")

    monkeypatch.setattr(runner, "place", limited)
    w._burst_buy(t, "yes", "burst: test", {})
    rows = store.db.execute("SELECT mode, side, reason, context FROM decisions WHERE lane='burst_flip' ORDER BY id").fetchall()
    assert [r["mode"] for r in rows] == ["observe", "observe"]  # both attempts are logged, not lost
    assert "live order not filled: order failed: no fill at the limit" in rows[0]["reason"] and json.loads(rows[0]["context"])["miss"] == "order failed: no fill at the limit"
    assert "rate limited" in rows[1]["reason"]


def test_a_paper_hunt_and_a_live_one_are_the_same_size(tmp_path, monkeypatch):
    from kalshi_moonshot import buyer, runner

    w, store, spec, h = _queued_hunt(tmp_path, monkeypatch)
    live = []
    monkeypatch.setattr(runner, "place", lambda settings, client, st, shot, why, count, **kw: (live.append(count) or runner.Placed("bought", count, 0.4)))
    w._hunt_buy(h, spec, "no", 0.03, "burst hunt: test", {})
    monkeypatch.setattr(config.Settings, "is_live", lambda self, lane, series="": False)
    paper = watcher.Hunt("KXSOL15M-1", "guarded", close_ts=time.time() + 100)
    w._hunt_buy(paper, spec, "no", 0.03, "burst hunt: test", {})
    row = store.db.execute("SELECT contracts FROM decisions WHERE ticker='KXSOL15M-1'").fetchone()
    assert live == [10.0] and row["contracts"] == 10.0 == buyer.whole_contracts(0.5, 0.03)  # not 16.7 contracts on paper against 10 live


def test_the_flow_ledger_drops_a_market_that_stopped_trading(tmp_path, monkeypatch):
    w, _s, _store, _close = _watcher(tmp_path, monkeypatch)
    sec = int(time.time())
    w._flow["KXOLD15M-1"] = {sec - 900: [5.0, 5.0, 1.0, 1.0]}
    w._flow["KXLIVE15M-1"] = {sec - 2: [5.0, 5.0, 1.0, 1.0]}
    w.refresh()
    assert "KXOLD15M-1" not in w._flow and "KXLIVE15M-1" in w._flow


def _hunt_job(w, h, spec, side="no", ask=0.05):
    """What _on_hunt does when a trigger fires: mark the market fired and queue the buy."""
    h.fired = True
    w._buys.put(watcher.BuySignal("hunt", side, ask, "burst hunt: test", {}, hunt=h, spec=spec))


def _two_rows(store):
    return store.db.execute("SELECT id, mode, ask, contracts, stake_usd, reason, context FROM decisions WHERE ticker='KXETH15M-1'").fetchall()


def test_a_hunt_that_certainly_bought_nothing_may_fire_again_and_the_fill_upgrades_its_row(tmp_path, monkeypatch):
    from kalshi_moonshot import runner

    w, store, spec, h = _queued_hunt(tmp_path, monkeypatch)
    answers = iter([runner.Placed("bought NO 0/7 @ 0.0700"), runner.Placed("bought NO 7/7 @ 0.0600", 7.0, 0.45)])
    monkeypatch.setattr(runner, "place", lambda *a, **k: next(answers))
    _hunt_job(w, h, spec)
    w._drain_buys()
    rows = _two_rows(store)
    assert len(rows) == 1 and rows[0]["mode"] == "observe"  # the first miss is the paper fallback, as before
    assert h.fired is False and h.attempts == 1 and h.miss_row == rows[0]["id"] and h.retry_at > time.time()  # and the market may fire again
    _hunt_job(w, h, spec, ask=0.06)
    w._drain_buys()
    rows = _two_rows(store)
    assert len(rows) == 1  # the same row, not a second bet on the market
    assert rows[0]["mode"] == "trade" and rows[0]["contracts"] == 7.0 and rows[0]["stake_usd"] == pytest.approx(0.45) and rows[0]["ask"] == pytest.approx(0.06)
    ctx = json.loads(rows[0]["context"])
    assert ctx["attempt"] == 2 and ctx["first_ask"] == pytest.approx(0.05)
    assert h.fired is True  # a fill ends it


def test_a_hunt_stops_retrying_after_its_attempts_and_keeps_one_paper_row(tmp_path, monkeypatch):
    from kalshi_moonshot import runner

    w, store, spec, h = _queued_hunt(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(runner, "place", lambda *a, **k: (calls.append(1) or runner.Placed("bought NO 0/7 @ 0.0700")))
    for _ in range(spec.hunt_attempts):
        assert h.fired is False
        _hunt_job(w, h, spec)
        w._drain_buys()
    assert len(calls) == spec.hunt_attempts and h.fired is True  # spent
    rows = _two_rows(store)
    assert len(rows) == 1 and rows[0]["mode"] == "observe" and json.loads(rows[0]["context"])["attempt"] == spec.hunt_attempts


def test_a_miss_that_may_have_filled_is_never_retried(tmp_path, monkeypatch):
    from kalshi_moonshot import runner

    for note in ("order failed: ReadTimeout: timed out", "blocked by risk: daily loss", "skipped: order $0.10 is under the $0.25 minimum", "skipped: exchange 1 (where KXETH15M trades) has no cash"):
        w, store, spec, h = _queued_hunt(tmp_path, monkeypatch)
        monkeypatch.setattr(runner, "place", lambda *a, _n=note, **k: runner.Placed(_n))
        _hunt_job(w, h, spec)
        w._drain_buys()
        assert h.fired is True and h.attempts == 1, note  # one attempt: the order may have gone through, or can never go through
        store.db.execute("DELETE FROM decisions WHERE ticker='KXETH15M-1'")
        store.db.commit()


def test_a_rate_limited_or_unreadable_book_is_retried(tmp_path, monkeypatch):
    from kalshi_moonshot import runner
    from kalshi_moonshot.kalshi import KalshiRateLimited

    w, _store, spec, h = _queued_hunt(tmp_path, monkeypatch)

    def limited(*a, **k):
        raise KalshiRateLimited("429")

    monkeypatch.setattr(runner, "place", limited)
    _hunt_job(w, h, spec)
    w._drain_buys()
    assert h.fired is False and h.attempts == 1  # nothing was sent
    monkeypatch.setattr(runner, "place", lambda *a, **k: runner.Placed("skipped: could not read the book (timeout)"))
    _hunt_job(w, h, spec)
    w._drain_buys()
    assert h.fired is False and h.attempts == 2


def test_a_retry_for_another_row_or_after_a_restart_is_blocked(tmp_path, monkeypatch):
    from kalshi_moonshot import buyer, runner

    w, store, spec, h = _queued_hunt(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "place", lambda *a, **k: runner.Placed("bought NO 0/7 @ 0.0700"))
    _hunt_job(w, h, spec)
    w._drain_buys()
    monkeypatch.setattr(runner, "place", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not buy twice")))
    fresh = watcher.Hunt("KXETH15M-1", "guarded", close_ts=time.time() + 100)  # after a restart the hunt knows nothing of the first row
    _hunt_job(w, fresh, spec)
    w._drain_buys()
    assert len(_two_rows(store)) == 1
    plan = buyer.hunt_plan(spec, "KXETH15M-1", "no", 0.05, fresh.close_ts, "r", {}, w.client)
    plan.retry_of = 999999  # a row that is not the one this market holds
    assert buyer.buy(plan, w.get_settings(), store).outcome == "skipped"


def test_a_stale_dropped_buy_lets_the_market_fire_again(tmp_path, monkeypatch):
    from kalshi_moonshot import runner

    w, store, spec, h = _queued_hunt(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "place", lambda *a, **k: (_ for _ in ()).throw(AssertionError("stale")))
    h.fired = True
    w._buys.put(watcher.BuySignal("hunt", "no", 0.05, "burst hunt: test", {}, hunt=h, spec=spec, queued=time.time() - spec.hunt_stale_seconds - 1.0))
    w._drain_buys()
    assert h.fired is False and h.attempts == 0 and _two_rows(store) == []  # nothing was tried, so no attempt is used


def test_a_hunt_waits_out_its_retry_cooldown(tmp_path, monkeypatch):
    import dataclasses

    from kalshi_moonshot import lanes

    w, s, _store, _close = _watcher(tmp_path, monkeypatch)
    spec = dataclasses.replace(lanes.lane_specs(s)["guarded"], name="guarded", price=(0.02, 0.15), hunt_dollars=400.0, sides=("yes", "no"), fixed_stake=0.5, max_open=0)
    now = 1_000_000.0
    sec = int(now)
    h = watcher.Hunt("KXETH15M-1", "guarded", close_ts=now + 60.0, yes_bid=0.93, yes_ask=0.94)
    h.quotes.extend((now - 20.0 + i, 0.93, 0.94) for i in range(21))
    book = {sec: [0.0, 6000.0, 0.0, 455.0]}  # $455 against the favorite
    h.retry_at = now + 3.0
    w._on_hunt(h, spec, now, book, sec)
    assert w._buys.empty()  # still cooling down
    w._on_hunt(h, spec, now + 4.0, {sec + 4: book[sec]}, sec + 4)
    assert w._buys.qsize() == 1  # the burst is still on and the cooldown is over


def test_the_hunt_and_burst_settings_default_to_what_was_hard_coded():
    from kalshi_moonshot.lanes import LaneSpec

    assert (LaneSpec.hunt_favorite, LaneSpec.hunt_cheap, LaneSpec.hunt_window, LaneSpec.hunt_min_left) == (0.0, 0.50, 120.0, 10.0)  # no favorite requirement unless a lane sets one
    assert LaneSpec.hunt_spike == 0.0  # and no spike requirement: the dollar bar alone
    assert (LaneSpec.hunt_crash, LaneSpec.hunt_crash_seconds, LaneSpec.hunt_park_seconds, LaneSpec.hunt_park_high, LaneSpec.hunt_park_span) == (0.10, 12.0, 20.0, 0.10, 0.06)
    assert (LaneSpec.hunt_attempts, LaneSpec.hunt_retry_seconds, LaneSpec.hunt_stale_seconds) == (3, 3.0, 10.0)
    assert (LaneSpec.burst_favorite, LaneSpec.burst_window, LaneSpec.burst_min_left) == (0.90, 120.0, 10.0)


def test_the_hunt_and_burst_settings_come_from_strategies_toml(tmp_path, monkeypatch):
    from kalshi_moonshot import lanes

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(
        "[lane.crypto_moonshot]\nestimator='ladder'\nprice=[0.02,0.20]\nhours=[0.0,0.04]\nburst_hunt=true\nhunt_dollars=400\n"
        "hunt_favorite=0.80\nhunt_window=90\nhunt_attempts=1\nburst_favorite=0.85\n"
    )
    spec = lanes.lane_specs(config.load())["crypto_moonshot"]
    assert (spec.hunt_favorite, spec.hunt_window, spec.hunt_attempts, spec.burst_favorite) == (0.80, 90, 1, 0.85)
    assert spec.hunt_crash == 0.10  # the ones it does not name keep their defaults


def _dollar_hunt(tmp_path, monkeypatch, *, yes_bid, yes_ask, left=60.0, baseline=0.0, **spec_changes):
    """A hunt on a YES-priced market with $455 bought against the favorite in the last 3 s, and the cheap side sitting at its ask for 20 s. Returns what _on_hunt queued."""
    import dataclasses

    from kalshi_moonshot import lanes

    w, s, _store, _close = _watcher(tmp_path, monkeypatch)
    fields = {"name": "guarded", "price": (0.02, 0.20), "hunt_dollars": 400.0, "sides": ("no",), "fixed_stake": 0.5, "max_open": 0, **spec_changes}
    spec = dataclasses.replace(lanes.lane_specs(s)["guarded"], **fields)
    now = 1_000_000.0
    sec = int(now)
    h = watcher.Hunt("KXETH15M-1", "guarded", close_ts=now + left, yes_bid=yes_bid, yes_ask=yes_ask)
    h.quotes.extend((now - 20.0 + i, yes_bid, yes_ask) for i in range(21))
    book = {sec: [0.0, 6000.0, 0.0, 455.0]}
    for k in range(3, 123):  # the market's ordinary second on the cheap (NO) side: `baseline` dollars
        book[sec - k] = [0.0, 0.0, 0.0, baseline]
    w._on_hunt(h, spec, now, book, sec)
    return w._buys.qsize()


def test_the_favorite_floor_is_the_lanes_setting(tmp_path, monkeypatch):
    assert _dollar_hunt(tmp_path, monkeypatch, yes_bid=0.85, yes_ask=0.86) == 1  # NO costs 15c: with no floor set, the dollars and the price band decide
    assert _dollar_hunt(tmp_path, monkeypatch, yes_bid=0.85, yes_ask=0.86, hunt_favorite=0.90) == 0  # a lane that asks for a 90c favorite does not buy an 85c one
    assert _dollar_hunt(tmp_path, monkeypatch, yes_bid=0.85, yes_ask=0.86, hunt_favorite=0.80) == 1  # and 80c is enough for another
    assert _dollar_hunt(tmp_path, monkeypatch, yes_bid=0.93, yes_ask=0.94, hunt_favorite=0.95) == 0  # a stricter lane can ask for more


def test_the_hunt_window_is_the_lanes_setting(tmp_path, monkeypatch):
    assert _dollar_hunt(tmp_path, monkeypatch, yes_bid=0.93, yes_ask=0.94, left=90.0) == 1
    assert _dollar_hunt(tmp_path, monkeypatch, yes_bid=0.93, yes_ask=0.94, left=90.0, hunt_window=60.0) == 0  # too early for this lane
    assert _dollar_hunt(tmp_path, monkeypatch, yes_bid=0.93, yes_ask=0.94, left=20.0) == 1
    assert _dollar_hunt(tmp_path, monkeypatch, yes_bid=0.93, yes_ask=0.94, left=20.0, hunt_min_left=30.0) == 0  # too late for this lane


def test_the_cheap_side_ceiling_is_the_lanes_setting(tmp_path, monkeypatch):
    assert _dollar_hunt(tmp_path, monkeypatch, yes_bid=0.93, yes_ask=0.94) == 1  # NO costs 7c
    assert _dollar_hunt(tmp_path, monkeypatch, yes_bid=0.93, yes_ask=0.94, hunt_cheap=0.05) == 0  # this lane calls only a side under 5c cheap


def test_the_crash_and_parked_checks_are_the_lanes_settings(tmp_path, monkeypatch):
    import dataclasses

    from kalshi_moonshot import lanes

    _w, s, _store, _close = _watcher(tmp_path, monkeypatch)
    spec = dataclasses.replace(lanes.lane_specs(s)["guarded"], name="guarded")
    now = 1_000_000.0
    eased = watcher.deque([(now - 5.0, 0.90, 0.91), (now - 4.0, 0.93, 0.94)])  # NO eased from 10c to 7c, 3c, within 5 s
    assert watcher._hunt_crashing(eased, "no", now, 0.07)[0] is False  # the default needs 10c
    assert watcher._hunt_crashing(eased, "no", now, 0.07, 0.02, 12.0)[0] is True  # a lane that calls 2c a crash
    assert watcher._hunt_crashing(eased, "no", now, 0.07, 0.02, 3.0)[0] is False  # ...but only looks back 3 s: that quote is older
    sat = watcher.deque((now - 20.0 + i, 0.93, 0.94) for i in range(21))
    assert watcher._hunt_parked(sat, "no", now, 0.07) is True
    assert watcher._hunt_parked(sat, "no", now, 0.07, spec.price[1], 40.0) is False  # a lane that wants 40 s on screen: only 20 s of quotes
    high = watcher.deque((now - 20.0 + i, 0.80, 0.81) for i in range(21))  # NO sat at 19c for 20 s and is now 7c: it fell, so it is not parked at the price we would pay
    assert watcher._hunt_parked(high, "no", now, 0.07, 0.30, 20.0, 0.30, 0.06) is False  # 19c is 12c over the 7c ask, past the default 6c span
    assert watcher._hunt_parked(high, "no", now, 0.07, 0.30, 20.0, 0.30, 0.15) is True  # a lane that allows a 15c span accepts it


def test_the_retry_limit_is_the_lanes_setting(tmp_path, monkeypatch):
    import dataclasses

    from kalshi_moonshot import runner

    w, _store, spec, h = _queued_hunt(tmp_path, monkeypatch)
    monkeypatch.setattr(runner, "place", lambda *a, **k: runner.Placed("bought NO 0/7 @ 0.0700"))
    _hunt_job(w, h, dataclasses.replace(spec, hunt_attempts=1))
    w._drain_buys()
    assert h.fired is True and h.attempts == 1  # this lane allows one try


def test_the_burst_exit_favorite_and_window_are_the_lanes_settings(tmp_path, monkeypatch):
    w, _s, _store, close = _watcher(tmp_path, monkeypatch)
    t = w.targets[("KXBTC15M-1", "guarded")]
    t.burst_bar = 400.0
    t.cur_bid, t.cur_other_ask, t.side = 0.85, 0.16, "yes"  # an 85c favorite
    now = close - 60
    sec = int(now)
    w._flow["KXBTC15M-1"] = {sec: [0.0, 6000.0, 0.0, 455.0]}
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now, "count": 10, "taker": "no", "price": 0.84})
    assert w._jobs.empty()  # under the default 90c favorite: not sold
    t.burst_favorite = 0.80
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now, "count": 10, "taker": "no", "price": 0.84})
    assert w._jobs.get_nowait().why.startswith("burst: $4")
    t.burst_logged = False
    t.burst_window = 30.0  # this lane only sells in the last 30 s, and 60 remain
    w.on_event({"kind": "trade", "ticker": "KXBTC15M-1", "ts": now, "count": 10, "taker": "no", "price": 0.84})
    assert w._jobs.empty()


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


def test_a_resting_buy_never_sits_at_or_over_the_ask_it_would_cross(tmp_path, monkeypatch):
    # read at 0.82 for NO, but the book now offers NO at 0.80 (best YES bid 0.20): 0.81 would have crossed and been refused
    sent, out = _resting(tmp_path, monkeypatch, book=_book(yes_bids=(0.20,)))
    assert sent == [("no", 2.0, pytest.approx(0.79))]  # 1c under the ask as it is now
    assert out.filled == 2.0 and out.price == pytest.approx(0.79)


def test_a_resting_buy_keeps_its_price_when_the_ask_has_not_fallen(tmp_path, monkeypatch):
    sent, out = _resting(tmp_path, monkeypatch, book=_book(yes_bids=(0.18,)), fill_yes_price="0.1900")  # NO is offered at 0.82, as read
    assert sent == [("no", 2.0, pytest.approx(0.81))] and out.price == pytest.approx(0.81)


def test_a_resting_buy_is_not_raised_when_the_ask_has_risen(tmp_path, monkeypatch):
    sent, _out = _resting(tmp_path, monkeypatch, book=_book(yes_bids=(0.10,)), fill_yes_price="0.1900")  # NO is offered at 0.90 now
    assert sent == [("no", 2.0, pytest.approx(0.81))]  # still the price the lane planned: a higher one would only cost more


def test_a_resting_buy_is_skipped_when_the_ask_has_left_the_lanes_band(tmp_path, monkeypatch):
    sent, out = _resting(tmp_path, monkeypatch, book=_book(yes_bids=(0.30,)))  # NO is offered at 0.70, under the lane's 0.76 floor
    assert sent == [] and out.filled == 0 and out.note.startswith("skipped:") and "0.70" in out.note and "0.82" in out.note and "0.76" in out.note


def test_a_resting_buy_with_no_book_is_priced_as_before(tmp_path, monkeypatch):
    sent, out = _resting(tmp_path, monkeypatch, book=None, fill_yes_price="0.1900")
    assert sent == [("no", 2.0, pytest.approx(0.81))] and out.price == pytest.approx(0.81)
    sent, out = _resting(tmp_path, monkeypatch, book=_book(), fill_yes_price="0.1900")  # an empty book has no ask to cross
    assert sent == [("no", 2.0, pytest.approx(0.81))]


def test_a_repriced_resting_buy_rests_its_take_profit_from_the_price_it_paid(tmp_path, monkeypatch):
    import dataclasses
    from types import SimpleNamespace

    from kalshi_moonshot import lanes, runner
    from kalshi_moonshot.strategy import LongShot

    _w, s, store, _close = _watcher(tmp_path, monkeypatch)
    spec = dataclasses.replace(lanes.lane_specs(s)["guarded"], buy_rest_seconds=20.0, buy_rest_below=0.01, price=(0.76, 0.92))
    monkeypatch.setattr(runner, "lane_specs", lambda settings: {"guarded": spec})
    book = _book(yes_bids=(0.20,))  # NO is offered at 0.80 now; the shot below was read at 0.82

    class C:
        def orderbook(self, ticker):
            return book

    monkeypatch.setattr(runner.risk, "check", lambda *a, **k: SimpleNamespace(ok=True, reason=""))
    monkeypatch.setattr(runner, "_top_up", lambda *a, **k: None)
    seen = {}

    def fake_resting(settings, client, st, shot, why, count, sp, key, live_book=None):
        seen["book"] = live_book
        return runner.Placed("bought NO 2/2 resting at 0.79", 2.0, 1.58, 0.79)

    entries = []
    monkeypatch.setattr(runner, "_place_resting", fake_resting)
    monkeypatch.setattr(runner, "_rest_take_profit", lambda settings, client, st, shot, sp, filled, entry: (entries.append(entry) or ""))
    shot = LongShot("KXBTC15M-9", "t", "", 0.81, 0.82, 10.0, "2099-01-01T00:00:00Z", 0.1, lane="guarded", series="KXBTC15M", side="no")
    out = runner.place(s, C(), store, shot, "why", 2.0)
    assert out.filled == 2.0
    assert seen["book"] is book  # the book place() already read is the one the resting buy is priced against
    assert entries == [pytest.approx(0.79)]  # the take-profit counts from what was paid, not from the planned 0.81


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


def test_a_post_only_cross_is_tried_once_more_against_a_fresh_book(tmp_path, monkeypatch):
    # the book read first still showed NO at 0.83; by the time the order landed it was 0.82 (best YES bid 0.18) and 0.82 crossed
    sent, out, reads = _resting_retry(tmp_path, monkeypatch, first_book=_book(yes_bids=(0.17,)), fresh_book=_book(yes_bids=(0.18,)), answers=[_cross(), {"order_id": "o1"}])
    assert sent == [pytest.approx(0.82), pytest.approx(0.81)]  # the second order rests 1c under the ask as the fresh book has it
    assert reads == ["KXBTC15M-9"] and out.filled == 2.0 and out.price == pytest.approx(0.81)


def test_a_second_post_only_cross_is_a_refusal_not_a_loop(tmp_path, monkeypatch):
    sent, out, _reads = _resting_retry(tmp_path, monkeypatch, first_book=_book(yes_bids=(0.17,)), fresh_book=_book(yes_bids=(0.18,)), answers=[_cross(), _cross(), {"order_id": "never"}])
    assert len(sent) == 2 and out.filled == 0 and out.note.startswith("resting buy refused") and "post only cross" in out.note


def test_a_refusal_that_is_not_a_cross_is_not_retried(tmp_path, monkeypatch):
    from kalshi_moonshot.kalshi import KalshiError

    sent, out, reads = _resting_retry(tmp_path, monkeypatch, first_book=_book(yes_bids=(0.17,)), fresh_book=_book(yes_bids=(0.18,)), answers=[KalshiError("POST /portfolio/events/orders -> 400: insufficient_balance")])
    assert len(sent) == 1 and reads == [] and out.note.startswith("resting buy refused") and out.filled == 0


def test_a_retry_after_a_cross_skips_if_the_fresh_ask_has_left_the_band(tmp_path, monkeypatch):
    sent, out, _reads = _resting_retry(tmp_path, monkeypatch, first_book=_book(yes_bids=(0.17,)), fresh_book=_book(yes_bids=(0.30,)), answers=[_cross(), {"order_id": "never"}])
    assert len(sent) == 1 and out.filled == 0 and out.note.startswith("skipped:") and "0.70" in out.note  # NO is 0.70 now, under the 0.76 floor


def test_a_retry_that_cannot_read_the_book_reports_the_refusal(tmp_path, monkeypatch):
    sent, out, _reads = _resting_retry(tmp_path, monkeypatch, first_book=_book(yes_bids=(0.17,)), answers=[_cross(), {"order_id": "never"}], fresh_error=True)
    assert len(sent) == 1 and out.filled == 0 and out.note.startswith("resting buy refused") and "post only cross" in out.note


def test_the_retry_after_a_cross_uses_a_new_client_order_id(tmp_path, monkeypatch):
    # the scan path gives every order a stable key. Kalshi remembers the id of the order it rejected, so sending the same key again is a duplicate (409) and the retry never lands
    ids = []
    sent, out, _reads = _resting_retry(tmp_path, monkeypatch, first_book=_book(yes_bids=(0.17,)), fresh_book=_book(yes_bids=(0.18,)), answers=[_cross(), {"order_id": "o1"}], order_key="KXBTC15M-9-guarded", ids=ids)
    assert len(sent) == 2 and out.filled == 2.0  # the retry went through
    assert ids[0] == "KXBTC15M-9-guarded" and ids[1] != ids[0] and ids[1].startswith("KXBTC15M-9-guarded")  # a different id, derived from the same key so a re-run of the job repeats it, not doubles up


def test_a_spike_is_the_multiple_of_the_sides_own_normal_not_a_fixed_number(tmp_path, monkeypatch):
    # $455 bought against the favorite in 3 s. On a quiet market (a normal second of $50) that is 3x normal; on a busy one ($300 a second) it is half of a normal 3 s.
    assert _dollar_hunt(tmp_path, monkeypatch, yes_bid=0.93, yes_ask=0.94, baseline=50.0) == 1  # no spike requirement: the bar alone fires
    assert _dollar_hunt(tmp_path, monkeypatch, yes_bid=0.93, yes_ask=0.94, baseline=300.0) == 1  # even on a market where $455 is below its usual 3 s
    assert _dollar_hunt(tmp_path, monkeypatch, yes_bid=0.93, yes_ask=0.94, baseline=300.0, hunt_spike=3.0) == 0  # a spike lane does not buy that
    assert _dollar_hunt(tmp_path, monkeypatch, yes_bid=0.93, yes_ask=0.94, baseline=50.0, hunt_spike=3.0) == 1  # 455 / (3 x 50) is 3.03x: a spike
    assert _dollar_hunt(tmp_path, monkeypatch, yes_bid=0.93, yes_ask=0.94, baseline=50.0, hunt_spike=5.0) == 0  # but not 5x
    assert _dollar_hunt(tmp_path, monkeypatch, yes_bid=0.93, yes_ask=0.94, baseline=0.0, hunt_spike=5.0) == 1  # a side with no normal buying at all: anything is a spike


def test_the_spike_still_needs_the_dollar_bar(tmp_path, monkeypatch):
    # 20x its own normal is a spike, but $455 is the whole of it only because the normal is $7.6 a second; a lane with a $1,000 bar still waits
    assert _dollar_hunt(tmp_path, monkeypatch, yes_bid=0.93, yes_ask=0.94, baseline=7.6, hunt_spike=5.0, hunt_dollars=1000.0) == 0


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


def test_a_burst_where_the_held_side_is_also_being_bought_is_not_a_crash(tmp_path, monkeypatch):
    # the setting off: any $455 against the holder sells, however hard the holder's own side is being bought
    assert _burst_exit(tmp_path, monkeypatch, with_holder=0.0, share=0.0) == 1
    assert _burst_exit(tmp_path, monkeypatch, with_holder=1500.0, share=0.0) == 1
    # the holder's side pumped ($1,500 with it against $455 against: 23% of the dollars): a lane that wants a one-sided burst holds
    assert _burst_exit(tmp_path, monkeypatch, with_holder=1500.0, share=0.75) == 0
    # one-sided ($455 against, $50 with: 90%) sells
    assert _burst_exit(tmp_path, monkeypatch, with_holder=50.0, share=0.75) == 1
    assert _burst_exit(tmp_path, monkeypatch, with_holder=0.0, share=0.75) == 1  # nothing bought with the holder at all


def test_the_burst_share_defaults_to_off():
    from kalshi_moonshot.lanes import LaneSpec

    assert LaneSpec.burst_share == 0.0


def _spot_tape(tmp_path, rows, symbol="coinbase:BTC-USD"):
    from kalshi_moonshot import db

    class S:
        tape_path = str(tmp_path / "tape.db")

    tape = db.connect(S.tape_path, "tape", shared=True)
    tape.execute("CREATE TABLE IF NOT EXISTS feed_ticks (ts REAL NOT NULL, symbol TEXT NOT NULL, px REAL NOT NULL)")
    for ts, px in rows:
        tape.execute("INSERT INTO feed_ticks (ts, symbol, px) VALUES (?,?,?)", (ts, symbol, px))
    tape.commit()
    return S


def _shot(side):
    from kalshi_moonshot.strategy import LongShot

    return LongShot("KXBTC15M-1", "", "", 0.74, 0.76, 0.0, "", 0.1, side=side, series="KXBTC15M")


def test_spot_ticks_must_all_go_the_way_of_the_bet(tmp_path):
    from kalshi_moonshot import runner

    S = _spot_tape(tmp_path, [(100.0, 100.0), (101.0, 100.5), (102.0, 100.5), (103.0, 101.0), (104.0, 101.2)])  # changes (a repeated price skipped): +0.5, +0.5, +0.2
    assert runner.spot_ticks_aligned(S, _shot("yes"), 3, now=105.0) is True
    assert runner.spot_ticks_aligned(S, _shot("no"), 3, now=105.0) is False  # a DOWN bet needs the coin falling
    assert runner.spot_ticks_aligned(S, _shot("yes"), 4, now=105.0) is None  # only three changes on the tape: no opinion
    (tmp_path / "b").mkdir()
    S2 = _spot_tape(tmp_path / "b", [(100.0, 100.0), (101.0, 100.4), (102.0, 100.2), (103.0, 100.6)])
    assert runner.spot_ticks_aligned(S2, _shot("yes"), 3, now=104.0) is False  # up, down, up: not three ups


def test_a_stale_spot_price_gives_no_opinion(tmp_path):
    from kalshi_moonshot import runner

    S = _spot_tape(tmp_path, [(100.0, 100.0), (101.0, 100.5), (102.0, 101.0), (103.0, 101.5)])
    assert runner.spot_ticks_aligned(S, _shot("yes"), 3, now=104.0) is True
    assert runner.spot_ticks_aligned(S, _shot("yes"), 3, now=300.0) is None  # the last price is minutes old
    assert runner.spot_move(S, _shot("yes"), 2.0, now=300.0) is None


def test_spot_move_is_signed_by_the_bets_direction(tmp_path):
    from kalshi_moonshot import runner

    S = _spot_tape(tmp_path, [(100.0, 100.0), (103.0, 99.0), (106.0, 101.0)])
    assert runner.spot_move(S, _shot("yes"), 5.0, now=107.0) == pytest.approx(1.0)  # 100 five seconds back (the tick at 100), 101 now: up 1
    assert runner.spot_move(S, _shot("no"), 5.0, now=107.0) == pytest.approx(-1.0)  # the same rise is against a DOWN bet
    assert runner.spot_move(S, _shot("yes"), 50.0, now=107.0) is None  # nothing that far back


def test_the_spot_check_falls_back_to_the_instruments_own_feed(tmp_path):
    from kalshi_moonshot import runner

    S = _spot_tape(tmp_path, [(100.0, 100.0), (101.0, 100.5), (102.0, 101.0), (103.0, 101.5)], symbol="kraken:BTC/USD")
    assert runner.spot_ticks_aligned(S, _shot("yes"), 3, now=104.0) is True


def test_a_lane_without_the_spot_settings_ignores_them():
    from kalshi_moonshot.lanes import LaneSpec

    assert LaneSpec.entry_spot_ticks == 0 and LaneSpec.entry_spot_seconds == 0.0  # off unless a lane sets them


def test_the_energy_moonshot_buys_a_cheap_side_that_just_fell_and_has_not_sat_there(tmp_path, monkeypatch):
    """The coin moonshot refuses a side whose ask just fell or has not been parked; the energy one takes the dollar bar alone (scripts/moonshot_funnel_study.py)."""
    import dataclasses

    from kalshi_moonshot import lanes

    w, s, _store, _close = _watcher(tmp_path, monkeypatch)
    toml = (
        "[lane.coins]\nestimator='ladder'\nprice=[0.02,0.20]\nhours=[0.0,0.04]\nalways=true\nburst_hunt=true\nseries=['KXBTC15M']\nsides=['yes','no']\nfixed_stake=0.5\nstop_loss=false\n"
        "hunt_dollars=400\nhunt_spike=5\n"
        "[lane.energy]\ninherit='coins'\nseries=['KXNATGAS15M']\nhunt_spike=0\nhunt_crash=1.0\nhunt_park_seconds=5\nhunt_park_high=1.0\nhunt_park_span=1.0\n"
    )
    specs = lanes.lane_specs_from(s, toml)
    bought = []
    _buy_inline(w)
    monkeypatch.setattr(w, "_hunt_buy", lambda h, sp, side, ask, reason, ctx: bought.append((sp.name, side, round(ask, 2))))
    now = 1_000_000.0
    sec = int(now)
    book = {sec: [0.0, 9000.0, 0.0, 500.0]}  # NO takers bought $500 in the last second: over the $400 bar, however thin the book was
    def hunt(lane):
        h = watcher.Hunt("KXNATGAS15M-1", lane, close_ts=now + 60.0, yes_bid=0.84, yes_ask=0.85)  # NO costs 0.16
        h.quotes.extend((now - 8.0 + i, 0.55, 0.56) for i in range(7))  # NO was 0.44 a few seconds ago ...
        h.quotes.append((now, 0.84, 0.85))  # ... and is 0.16 now: a fall of 28c, and it has not been under 20c for 20 s
        return h
    w._on_hunt(hunt("coins"), dataclasses.replace(specs["coins"], name="coins"), now, book, sec)
    assert bought == []  # crash and parked checks refuse it
    w._on_hunt(hunt("energy"), dataclasses.replace(specs["energy"], name="energy"), now, book, sec)
    assert bought == [("energy", "no", 0.16)]  # the dollar bar alone is enough
    assert specs["energy"].series == ("KXNATGAS15M",) and specs["energy"].burst_hunt  # and it is the same kind of lane, on its own series
