"""The ladder's width for a market priced from the Kalshi perp is the last HOUR of that perp and nothing else (2026-11-14).
Until then it was six hours of 5-minute bars times a multiplier refitted hourly on six hours of outcomes (widthfit.py, removed). After a one-way afternoon the multiplier was 0.625, a gap of 0.14 of a real move was read as 0.4, and the lane bought KXBTC15M-26NOV141645 NO at 0.55 on a "65%" chance; it lost. User: "These markets shift in behavior hourly if you're looking at 6 hours to base a decision on right now that is totally irrelevant"."""
import math
import random
import time
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_moonshot import commodities as cm
from kalshi_moonshot import curverisk
from kalshi_moonshot import ticks as ticks_mod
from kalshi_moonshot.kalshifeed import KalshiPerpFeed
from kalshi_moonshot.tape import Tape
from tests.test_commodities import settings

SERIES = "KXBTC15M"
QUIET, WILD = 0.2e-4, 2.0e-4  # the one-second move: a typical minute of about 1.5 bp and of about 15 bp


@pytest.fixture(autouse=True)
def _no_memory():
    for name in ("_ts", "_v", "_vals", "_from"):
        got = getattr(ticks_mod.history, name, None)
        if isinstance(got, dict):
            got.clear()
    yield


def world(tmp_path, earlier, last_hour, hours=7.0, seed=7):
    """A perp tick a second for `hours`: `earlier` sized moves until an hour ago, `last_hour` sized moves since."""
    rng, now = random.Random(seed), time.time()
    n, price, rows = int(hours * 3600), 100000.0, []
    for k in range(n):
        price *= math.exp(rng.gauss(0.0, last_hour if n - k <= 3600 else earlier))
        rows.append((now - (n - k), "BRTI", price, None, None, None))
    tape = Tape(tmp_path / "tape.db")
    tape.db.executemany("INSERT INTO index_ticks VALUES (?,?,?,?,?,?)", rows)
    tape.db.commit()
    return KalshiPerpFeed(tape.db), datetime.fromtimestamp(now, UTC)


def priced(tmp_path, feed, now, minutes=9.0, **over):
    s = settings(tmp_path, **over)
    inst = cm.instrument_for(SERIES, cm.load_instruments(s.data_dir))
    assert inst.symbol.startswith("kp:")
    event = [{"series_ticker": SERIES, "close_time": (now + timedelta(minutes=minutes)).isoformat()}]
    basis = {SERIES: cm.WindowBasis(0.0, 0.0001, 15, now.timestamp())}  # the offset of the perp to the settlement reference, as learned from the last windows: 1 bp of uncertainty
    return cm.price_event(event, inst, feed, s, now, windows=basis)


# ---- unit ----
def test_unit_hour_rate_is_the_root_mean_square_of_the_hours_one_minute_moves():
    now = 10_000.0
    ts = [now - 60.0 * k for k in range(60, -1, -1)]
    vs = [100.0 * math.exp(0.001 * (k % 2)) for k in range(61)]  # every minute a move of 10 bp, up then down
    assert curverisk.hour_rate(ts, vs, now) == pytest.approx(0.001 / math.sqrt(60.0), rel=1e-6)
    assert curverisk.hour_rate(ts[-20:], vs[-20:], now) is None  # under half an hour on record: no reading
    assert curverisk.hour_rate([], [], now) is None


def test_unit_the_btc_bet_of_16_45_reads_as_a_coin_flip():
    """7.2 bp under the strike, a typical minute of 16.5 bp, 554 s left: 0.14 of a move, about 56%. The lane had called it 65%."""
    sigma = 16.5e-4 * math.sqrt(554.0 / 60.0)
    p_yes = cm.prob_above(64478.0, 64524.70, sigma)
    assert 1.0 - p_yes == pytest.approx(0.557, abs=0.01)


# ---- integration: the real pricer on a real tape ----
def test_integration_the_width_is_the_last_hours_movement_over_the_time_left(tmp_path):
    feed, now = world(tmp_path, WILD, WILD)
    got = priced(tmp_path, feed, now, minutes=9.0)
    ts, vs = feed.ticks("kp:BRTI", 1.1)
    assert got.sigma >= curverisk.hour_rate(ts, vs, now.timestamp()) * math.sqrt(540.0) * 0.999  # the hour's rate over 540 s, plus only the uncertainty of the center
    assert got.sigma == pytest.approx(WILD * math.sqrt(540.0), rel=0.25)


def test_integration_what_happened_before_the_last_hour_does_not_count(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    calm_now, now = world(tmp_path / "a", WILD, QUIET)  # six wild hours, then a calm one
    wild_now, now2 = world(tmp_path / "b", QUIET, WILD)  # six calm hours, then a wild one
    calm = priced(tmp_path / "a", calm_now, now)
    wild = priced(tmp_path / "b", wild_now, now2)
    assert wild.sigma > 4.0 * calm.sigma  # the reading follows the last hour, whichever way the six before it went


def test_integration_no_multiplier_touches_it(tmp_path):
    feed, now = world(tmp_path, WILD, WILD)
    assert priced(tmp_path, feed, now, commodity_vol_mult=1.0).sigma == pytest.approx(priced(tmp_path, feed, now, commodity_vol_mult=2.5).sigma)


def test_integration_with_under_half_an_hour_of_the_perp_the_market_is_not_priced(tmp_path):
    feed, now = world(tmp_path, WILD, WILD, hours=0.3)
    with pytest.raises(cm.CommodityError):
        priced(tmp_path, feed, now)


def test_integration_the_width_grows_with_the_time_left(tmp_path):
    feed, now = world(tmp_path, WILD, WILD)
    assert priced(tmp_path, feed, now, minutes=12.0).sigma > priced(tmp_path, feed, now, minutes=3.0).sigma
