"""The hour's own correction to the ladder's width, fitted from the windows that settled in the last hour, every perp-priced series pooled (widthfit.py), and the pricer uses it (2026-11-14: it was six hours per series; the user: "it should be based on the current hour. 2 hours at most")."""
import math
import random
import time
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_moonshot import commodities as cm
from kalshi_moonshot import widthfit
from kalshi_moonshot.kalshifeed import KalshiPerpFeed
from kalshi_moonshot.tape import Tape
from tests.test_commodities import settings

SERIES = "KXBTC15M"
FLOOR = 0.005


def test_the_fit_finds_the_width_the_outcomes_were_drawn_from():
    rng = random.Random(1)
    sig, true_k = 1.0e-3, 0.75
    samples = []
    for _ in range(600):
        lnd = rng.gauss(0.0, 2.0e-3)
        p = cm.prob_above(math.exp(lnd), 1.0, true_k * sig)
        samples.append((lnd, sig, 0.0, 1 if rng.random() < p else 0))
    k = widthfit.best_k(samples, default=1.25, floor=FLOOR)
    assert abs(k - true_k) <= 0.25 and k < 1.25  # the grid is 0.125 apart; a 600-reading draw is near its truth, and well under the global 1.25


def test_too_few_readings_keep_the_global_multiplier():
    assert widthfit.best_k([(0.001, 0.001, 0.0, 1)] * (widthfit.MIN_SAMPLES - 1), default=1.25, floor=FLOOR) == 1.25


def test_wider_than_the_outcomes_is_penalised_and_so_is_narrower():
    rng = random.Random(2)
    samples = []
    for _ in range(500):
        lnd = rng.gauss(0.0, 2.0e-3)
        samples.append((lnd, 1.0e-3, 0.0, 1 if rng.random() < cm.prob_above(math.exp(lnd), 1.0, 1.0e-3) else 0))
    assert widthfit.log_loss(samples, 1.0, FLOOR) < widthfit.log_loss(samples, 2.0, FLOOR)
    assert widthfit.log_loss(samples, 1.0, FLOOR) < widthfit.log_loss(samples, 0.5, FLOOR)


def _world(tmp_path, hours=13.0):
    """Perp ticks (one a second, a random walk with a five-minute move of about 15 bp) up to now, and the 40 most recent settled windows of KXBTC15M with their strike and settlement reference."""
    rng = random.Random(7)
    now = time.time()
    n = int(hours * 3600)
    price, rows, px = 100000.0, [], {}
    for k in range(n):
        price *= math.exp(rng.gauss(0.0, 0.87e-4))
        t = now - (n - k)
        rows.append((t, "BRTI", price, None, None, None))
        px[int(t)] = price
    tape = Tape(tmp_path / "tape.db")
    tape.db.executemany("INSERT INTO index_ticks VALUES (?,?,?,?,?,?)", rows)
    tape.db.commit()
    last_close = int(now) // 900 * 900 - 900
    markets = []
    for j in range(40):
        close = last_close - 900 * j
        strike, final = px.get(close - 900), px.get(close)
        markets.append({
            "ticker": f"{SERIES}-{j}", "series_ticker": SERIES, "floor_strike": strike, "expiration_value": str(final),
            "close_time": datetime.fromtimestamp(close, UTC).isoformat(), "result": "yes" if final > strike else "no",
        })
    return tape, markets, datetime.now(UTC)


class Settled:
    def __init__(self, markets):
        self.markets = markets

    def settled_markets(self, series, limit=40):
        return self.markets if series == SERIES else []


def test_unit_only_the_windows_that_closed_in_the_last_hour_are_read(tmp_path):
    tape, markets, now = _world(tmp_path)  # 40 settled windows, ten hours of them
    feed = KalshiPerpFeed(tape.db)
    ts, vs = feed.ticks("kp:BRTI", widthfit.FIT_HOURS + 4.5)
    samples = widthfit.samples_from(markets, ts, vs, now.timestamp(), 0.00015)
    assert 4 <= len(samples) <= 4 * len(widthfit.SAMPLE_MINUTES)  # at most the four windows of the hour, four readings each
    assert widthfit.FIT_HOURS <= 2.0  # the user: the current hour, two at most


def test_unit_each_reading_is_priced_from_the_hour_before_it(tmp_path):
    tape, markets, now = _world(tmp_path)
    feed = KalshiPerpFeed(tape.db)
    ts, vs = feed.ticks("kp:BRTI", widthfit.FIT_HOURS + 4.5)
    samples = widthfit.samples_from(markets, ts, vs, now.timestamp(), 0.00015)
    # the walk moves 0.87 bp a second, so the movement over the 60 to 600 s left at a reading is 0.87e-4 * sqrt(seconds): between 6.7 and 21 bp
    assert all(0.8 * 0.87e-4 * math.sqrt(60) <= sig <= 1.2 * 0.87e-4 * math.sqrt(600) for _, sig, _, _ in samples)


def _pricer(tmp_path, monkeypatch, min_samples=4):  # 4: sixteen minutes on, only one of the fixture's settled windows may still be inside the hour (it depends on where the clock sits in the quarter-hour; at 8 this test failed about one run in three)
    tape, markets, now = _world(tmp_path)
    monkeypatch.setattr(widthfit, "MIN_SAMPLES", min_samples)  # one series in the test: the live fit pools every perp-priced series to reach its 40
    s = settings(tmp_path, commodity_window_unc_floor=0.00015)
    feed = KalshiPerpFeed(tape.db)
    pricer = cm.CommodityPricer(feed)
    return pricer, feed, markets, now, s


def test_integration_the_pricer_fits_one_correction_from_the_last_hour_and_refits_once_a_window(tmp_path, monkeypatch):
    pricer, _feed, markets, now, s = _pricer(tmp_path, monkeypatch)
    assert pricer.learn_windows(Settled(markets), s, now) >= 1
    k, when, n = pricer.width_fit
    assert k in widthfit.GRID and n <= 4 * len(widthfit.SAMPLE_MINUTES) and when == pytest.approx(now.timestamp(), abs=5)
    assert pricer.hour_k(now.timestamp()) == k
    before = pricer.width_fit
    pricer._tried.clear()
    pricer.learn_windows(Settled(markets), s, now + timedelta(minutes=5))  # inside the same window: not refitted
    assert pricer.width_fit == before
    pricer._tried.clear()
    pricer.learn_windows(Settled(markets), s, now + timedelta(minutes=16))  # the next window: refitted
    assert pricer.width_fit[1] == pytest.approx((now + timedelta(minutes=16)).timestamp(), abs=5)


def test_integration_a_fit_older_than_two_windows_is_dropped(tmp_path, monkeypatch):
    pricer, _feed, markets, now, s = _pricer(tmp_path, monkeypatch)
    pricer.learn_windows(Settled(markets), s, now)
    pricer.width_fit = (0.5, now.timestamp(), 99)
    assert pricer.hour_k(now.timestamp() + widthfit.MAX_AGE - 5) == 0.5
    assert pricer.hour_k(now.timestamp() + widthfit.MAX_AGE + 5) == 1.0  # nothing fresh: the hour's own movement as it is


def test_integration_with_too_few_readings_in_the_hour_there_is_no_correction(tmp_path, monkeypatch):
    pricer, _feed, markets, now, s = _pricer(tmp_path, monkeypatch, min_samples=40)  # one series gives at most 16 readings an hour
    pricer.learn_windows(Settled(markets), s, now)
    assert pricer.width_fit is None and pricer.hour_k(now.timestamp()) == 1.0


def test_integration_the_correction_scales_the_hours_movement_in_the_width(tmp_path, monkeypatch):
    pricer, feed, markets, now, s = _pricer(tmp_path, monkeypatch)
    pricer.learn_windows(Settled(markets), s, now)
    inst = cm.instrument_for(SERIES, cm.load_instruments(s.data_dir))
    event = [{"series_ticker": SERIES, "close_time": (now + timedelta(minutes=7)).isoformat()}]
    plain = cm.price_event(event, inst, feed, s, now, windows=pricer.windows)
    wide = cm.price_event(event, inst, feed, s, now, windows=pricer.windows, hour_k=2.0)
    narrow = cm.price_event(event, inst, feed, s, now, windows=pricer.windows, hour_k=0.5)
    assert narrow.sigma < plain.sigma < wide.sigma
    assert plain.sigma == pytest.approx(0.87e-4 * math.sqrt(420.0), rel=0.3)  # the hour's movement over the 7 minutes left, uncorrected


def test_integration_a_feed_without_tick_history_is_left_alone(tmp_path):
    class NoTicks:
        def minute_closes(self, symbol, hours=4.0):
            return {}

    pricer = cm.CommodityPricer(NoTicks())
    assert pricer.learn_widths(settings(tmp_path), time.time()) is False and pricer.hour_k() == 1.0
