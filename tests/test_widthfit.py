"""A 15-minute series fits its own ladder width from its recent outcomes (widthfit.py), and the pricer uses it."""
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


def test_the_pricer_fits_the_series_width_hourly_from_its_settled_windows(tmp_path):
    tape, markets, now = _world(tmp_path)
    s = settings(tmp_path, commodity_width_fit=True, commodity_window_unc_floor=0.00015)
    pricer = cm.CommodityPricer(KalshiPerpFeed(tape.db))
    assert pricer.learn_windows(Settled(markets), s, now) >= 1
    k, when, n = pricer.widths[SERIES]
    assert k in widthfit.GRID and n >= widthfit.MIN_SAMPLES and when == pytest.approx(now.timestamp(), abs=5)
    again = dict(pricer.widths)
    pricer.learn_windows(Settled(markets), s, now + timedelta(minutes=20))  # fresh enough: not refitted
    assert pricer.widths == again


def test_a_fitted_multiplier_replaces_the_global_one_in_the_width(tmp_path):
    tape, markets, now = _world(tmp_path)
    s = settings(tmp_path, commodity_width_fit=True, commodity_window_unc_floor=0.00015)
    feed = KalshiPerpFeed(tape.db)
    pricer = cm.CommodityPricer(feed)
    pricer.learn_windows(Settled(markets), s, now)
    inst = cm.instrument_for(SERIES, cm.load_instruments(s.data_dir))
    event = [{"series_ticker": SERIES, "close_time": (now + timedelta(minutes=7)).isoformat()}]
    wide = cm.price_event(event, inst, feed, s, now, windows=pricer.windows, widths={SERIES: (2.0, now.timestamp(), 99)})
    narrow = cm.price_event(event, inst, feed, s, now, windows=pricer.windows, widths={SERIES: (0.5, now.timestamp(), 99)})
    plain = cm.price_event(event, inst, feed, s, now, windows=pricer.windows)
    assert narrow.sigma < plain.sigma < wide.sigma  # 0.5 < the global 1.25 < 2.0
    stale = cm.price_event(event, inst, feed, s, now, windows=pricer.windows, widths={SERIES: (0.5, now.timestamp() - cm.WIDTH_MAX_AGE - 60, 99)})
    assert stale.sigma == pytest.approx(plain.sigma)  # an old fit is not used


def test_the_switch_off_leaves_the_global_multiplier(tmp_path):
    tape, markets, now = _world(tmp_path)
    s = settings(tmp_path, commodity_width_fit=False, commodity_window_unc_floor=0.00015)
    pricer = cm.CommodityPricer(KalshiPerpFeed(tape.db))
    pricer.learn_windows(Settled(markets), s, now)
    assert pricer.widths == {}


def test_a_feed_without_tick_history_is_left_alone(tmp_path):
    class NoTicks:
        def minute_closes(self, symbol, hours=4.0):
            return {}

        def series(self, symbol):
            raise cm.CommodityError("none")

    s = settings(tmp_path, commodity_width_fit=True, commodity_window_unc_floor=0.00015)
    pricer = cm.CommodityPricer(NoTicks())
    assert pricer.learn_widths(s, time.time()) == 0
