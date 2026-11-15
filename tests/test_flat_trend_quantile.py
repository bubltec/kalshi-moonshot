"""The flat-trend skip's cutoff from the market's own history (2026-11-15, user: the fixed 0.05% should scale, not be an absolute number)."""
import math
import time

import pytest

from kalshi_moonshot import config, regime, runner, ticks, variants
from kalshi_moonshot.estimators import Estimate
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot

PERIOD = 14400.0  # a four-hour swing: the 60-minute move varies from zero to about 1.41 x the amplitude


def seed(now, amplitude, phase_at_now):
    """Every coin's index as a slow swing of `amplitude` (a fraction) for 7.5 hours up to `now`, with `phase_at_now` radians at now. Ticks every 30 s."""
    ticks.history.clear()
    variants._flat_memo.clear()
    variants._broad_memo = (0, None)
    t0 = now - phase_at_now / (2 * math.pi) * PERIOD
    for ser in variants.BROAD_SERIES:
        index = variants.crypto_index(ser)
        for k in range(int(7.5 * 3600 / 30) + 1):  # the last tick is at now: the trend is read only from fresh ticks
            t = now - 7.5 * 3600 + k * 30.0
            ticks.history.note(index, t, 100.0 * (1.0 + amplitude * math.sin(2 * math.pi * (t - t0) / PERIOD)))


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.history.clear()
    variants._flat_memo.clear()
    variants._broad_memo = (0, None)


def test_unit_the_cutoff_scales_with_how_much_the_market_has_been_moving():
    now = time.time()
    seed(now, 0.005, math.pi / 4)
    quiet = variants.broad_trend_bar(0.2, now)
    seed(now, 0.015, math.pi / 4)
    wild = variants.broad_trend_bar(0.2, now)
    assert quiet is not None and wild is not None and quiet > 0
    assert wild / quiet == pytest.approx(3.0, rel=0.05)  # three times the swing, three times the cutoff


def test_unit_the_cutoff_is_the_quantile_of_the_absolute_trend_and_a_larger_share_gives_a_larger_cutoff():
    now = time.time()
    seed(now, 0.005, math.pi / 4)
    series = variants.broad_trend_series(now)
    assert len(series) > 60
    small, big = variants.broad_trend_bar(0.1, now), variants.broad_trend_bar(0.5, now)
    assert 0 < small < big
    assert big == pytest.approx(sorted(abs(v) for v in series)[len(series) // 2], rel=0.05)


def test_unit_with_under_two_hours_of_history_there_is_no_cutoff_and_the_fixed_number_stays():
    now = time.time()
    ticks.history.clear()
    variants._flat_memo.clear()
    assert variants.broad_trend_bar(0.2, now) is None  # no history at all
    for ser in variants.BROAD_SERIES:
        index = variants.crypto_index(ser)
        for k in range(int(5400 / 30) + 130):  # about 1.5 hours plus the hour the trend looks back
            ticks.history.note(index, now - 7200.0 + k * 30.0, 100.0 + 0.001 * k)
    variants._flat_memo.clear()
    assert variants.broad_trend_bar(0.2, now) is None


LANE = "[lane.fl]\nestimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\nbuy_flat_trend_quantile={q}\nbuy_flat_trend_pct={pct}\n"


class Fixed:
    def estimate(self, client, settings, shot):
        return Estimate(0.9, "fixed")


def decide(tmp_path, monkeypatch, now, q, pct):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(q=q, pct=pct))
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    shot = LongShot("KXBTC15M-7", "t", "", 0.88, 0.89, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="fl", series="KXBTC15M", side="yes")
    return runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})


def test_integration_a_trend_inside_the_markets_own_smallest_fifth_is_flat_and_the_buy_waits(tmp_path, monkeypatch):
    now = time.time()
    seed(now, 0.005, 3 * math.pi / 4)  # the 60-minute move is zero right now
    out = decide(tmp_path, monkeypatch, now, q=0.2, pct=0.05)
    assert out.action == "pass" and "is flat" in out.note and "smallest 20%" in out.note


def test_integration_a_trend_outside_the_markets_own_smallest_fifth_buys(tmp_path, monkeypatch):
    now = time.time()
    seed(now, 0.005, math.pi / 4)  # the 60-minute move is at its largest right now, about +0.7%
    out = decide(tmp_path, monkeypatch, now, q=0.2, pct=0.05)
    assert out.action == "bet", out.note


def test_integration_the_same_trend_is_flat_on_a_wild_day_and_not_on_a_quiet_one(tmp_path, monkeypatch):
    """The point of scaling: a 0.3% hour is ordinary on a quiet day and flat on a wild one, where the fixed 0.05% called neither flat."""
    now = time.time()
    # the hour's move is 100 x amplitude x 1.414 x sin(phase + pi/4); phases chosen so it is 0.30% in both
    seed(now, 0.0043, math.asin(0.30 / (100 * 0.0043 * 1.414)) - math.pi / 4)
    quiet = variants.broad_trend_now(now)
    assert quiet is not None and 0.25 < abs(quiet) < 0.35
    assert decide(tmp_path, monkeypatch, now, q=0.2, pct=0.05).action == "bet"
    seed(now, 0.0215, math.asin(0.30 / (100 * 0.0215 * 1.414)) - math.pi / 4)
    wild = variants.broad_trend_now(now)
    assert wild is not None and 0.25 < abs(wild) < 0.35
    out = decide(tmp_path, monkeypatch, now, q=0.2, pct=0.05)
    assert out.action == "pass" and "is flat" in out.note  # 0.3% is inside the smallest fifth of a wild day


def test_integration_with_no_history_the_fixed_number_decides_as_before(tmp_path, monkeypatch):
    now = time.time()
    seed(now, 0.005, math.pi / 4)
    # only the last 70 minutes of ticks: the trend can be read but the six hours cannot
    for ser in variants.BROAD_SERIES:
        ticks.history.clear()
    for ser in variants.BROAD_SERIES:
        index = variants.crypto_index(ser)
        for k in range(int(4300 / 30) + 1):
            ticks.history.note(index, now - 4300.0 + k * 30.0, 100.0 + 0.00001 * k)  # a trend of about 0.012%: inside the fixed 0.05
    variants._flat_memo.clear()
    variants._broad_memo = (0, None)
    out = decide(tmp_path, monkeypatch, now, q=0.2, pct=0.05)
    assert out.action == "pass" and "is flat" in out.note and "fixed cutoff" in out.note
