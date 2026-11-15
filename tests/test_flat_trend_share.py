"""The flat-trend skip's cutoff from the market's own CURRENT hour (2026-11-15, user: the fixed 0.05% should scale; and not from six hours, the current hour)."""
import math
import time

import pytest

from kalshi_moonshot import config, regime, runner, ticks, variants
from kalshi_moonshot.estimators import Estimate
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot


def seed(now, drift_pct_per_hour, swing_pct, seconds=3900.0, step=30.0):
    """Every coin's index for `seconds` up to `now`: a steady drift of `drift_pct_per_hour` percent an hour plus a swing of +-`swing_pct` percent that flips every minute (so each one-minute change is about 2 x swing). Ticks every `step` seconds, the last at now."""
    ticks.history.clear()
    variants._hour_move_memo = (0, None)
    variants._broad_memo = (0, None)
    n = int(seconds / step)
    for ser in variants.BROAD_SERIES:
        index = variants.crypto_index(ser)
        for k in range(n + 1):
            t = now - seconds + k * step
            sign = 1.0 if int((t - now) // 60.0) % 2 == 0 else -1.0
            ticks.history.note(index, t, 100.0 * (1.0 + drift_pct_per_hour / 100.0 * (t - now) / 3600.0 + sign * swing_pct / 100.0))


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.history.clear()
    variants._hour_move_memo = (0, None)
    variants._broad_memo = (0, None)


def test_unit_the_hours_typical_move_comes_from_its_own_one_minute_moves_and_scales_with_them():
    now = time.time()
    seed(now, 0.0, 0.01)
    quiet = variants.broad_hour_move(now)
    seed(now, 0.0, 0.05)
    wild = variants.broad_hour_move(now)
    assert quiet is not None and wild is not None
    assert quiet == pytest.approx(100 * 0.0002 * math.sqrt(60), rel=0.1)  # a one-minute change of about 0.02% times sqrt(60)
    assert wild / quiet == pytest.approx(5.0, rel=0.1)


def test_unit_the_cutoff_is_the_share_of_the_hours_typical_move_and_a_net_drift_does_not_change_the_unit_much():
    now = time.time()
    seed(now, 0.0, 0.05)
    move = variants.broad_hour_move(now)
    assert variants.broad_trend_bar(0.25, now) == pytest.approx(0.25 * move)
    assert variants.broad_trend_bar(0.5, now) == pytest.approx(2 * variants.broad_trend_bar(0.25, now))


def test_unit_with_no_current_hour_there_is_no_cutoff():
    now = time.time()
    ticks.history.clear()
    variants._hour_move_memo = (0, None)
    assert variants.broad_hour_move(now) is None and variants.broad_trend_bar(0.25, now) is None
    seed(now, 0.0, 0.05, seconds=1800.0)  # only half an hour of ticks: the hour cannot be read
    variants._hour_move_memo = (0, None)
    assert variants.broad_hour_move(now) is None


LANE = "[lane.fl]\nestimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\nbuy_flat_trend_share={share}\nbuy_flat_trend_pct={pct}\n"


class Fixed:
    def estimate(self, client, settings, shot):
        return Estimate(0.9, "fixed")


def decide(tmp_path, monkeypatch, now, share, pct):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(share=share, pct=pct))
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    shot = LongShot("KXBTC15M-7", "t", "", 0.88, 0.89, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="fl", series="KXBTC15M", side="yes")
    return runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})


def test_integration_the_same_net_move_is_flat_in_a_choppy_hour_and_real_in_a_quiet_one(tmp_path, monkeypatch):
    """The point of scaling to the current hour: a 0.1% net move is a real one when the hour has barely moved around and flat when it has swung through ten times that. The fixed 0.05% called neither flat."""
    now = time.time()
    seed(now, 0.10, 0.01)
    quiet = decide(tmp_path, monkeypatch, now, share=0.25, pct=0.05)
    assert quiet.action == "bet", quiet.note
    seed(now, 0.10, 0.08)
    choppy = decide(tmp_path, monkeypatch, now, share=0.25, pct=0.05)
    assert choppy.action == "pass" and "is flat" in choppy.note and "of this hour's own typical move" in choppy.note


def test_integration_when_the_hour_cannot_be_read_the_fixed_number_decides_as_before(tmp_path, monkeypatch):
    now = time.time()
    ticks.history.clear()
    variants._hour_move_memo = (0, None)
    variants._broad_memo = (0, None)
    for ser in variants.BROAD_SERIES:  # a tick every 100 s: the trend can be read, but the minute marks cannot
        index = variants.crypto_index(ser)
        for k in range(int(4300 / 100) + 1):
            ticks.history.note(index, now - 4300.0 + k * 100.0, 100.0 + 0.00001 * k)  # a trend of about 0.003%: inside the fixed 0.05
    out = decide(tmp_path, monkeypatch, now, share=0.25, pct=0.05)
    assert out.action == "pass" and "is flat" in out.note and "fixed cutoff" in out.note
