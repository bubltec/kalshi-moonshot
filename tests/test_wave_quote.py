"""The wave rule read from the market's own Kalshi quote (lane field wave_quote): 2026-11-16, user: "i often see a YES that should have been a NO a few minutes later ... something with how we choose to buy is off"; studies/results/2026-11-16-entry-selection.txt."""
import dataclasses
import time

import pytest

from kalshi_moonshot import config, lanes, regime, runner, ticks, variants
from kalshi_moonshot.estimators import Estimate
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot

LANE = "[lane.wq]\nestimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\n{extra}\n"
LEFT = 400.0  # seconds to the close: the market opened 500 s ago


class Fixed:
    def estimate(self, client, settings, shot):
        return Estimate(0.9, "fixed")


def climb(now, opened, start=0.50, end=0.70, then=None):
    """(time, YES mid) rows, one a second from the open: a zig-zag climb from `start` to `end`, then an optional pull back to `then` over the last minute."""
    n = int(now - opened)
    rows = []
    for k in range(n + 1):
        mid = start + (end - start) * k / n + (0.004 if k % 2 else -0.004)
        if then is not None and k > n - 60:
            mid = end + (then - end) * (k - (n - 60)) / 60.0
        rows.append((opened + k, mid))
    return rows


def seed(rows, ticker="KXBTC15M-7"):
    ticks.mids.clear()
    for ts, mid in rows:
        ticks.mids.note(ticker, ts, mid - 0.005, mid + 0.005)


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.mids.clear()
    ticks.history.clear()


def decide(tmp_path, monkeypatch, now, extra, side="yes"):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(extra=extra))
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + LEFT, 100.0, now))
    store.db.commit()
    shot = LongShot("KXBTC15M-7", "t", "", 0.69, 0.70, LEFT / 3600.0, "2099-01-01T00:00:00Z", LEFT / 3600.0, lane="wq", series="KXBTC15M", side=side)
    return runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})


def test_unit_the_position_is_the_sides_place_in_its_own_range_since_the_open():
    now = time.time()
    opened = now - 500.0
    top = variants.quote_wave_features(climb(now, opened), "yes", opened, now)
    assert top["pos"] > 0.9 and top["amp"] > 0  # YES climbed all market: it is bought at its high
    low = variants.quote_wave_features(climb(now, opened), "no", opened, now)
    assert low["pos"] < 0.1  # the same market's NO sits at its low
    mid = variants.quote_wave_features(climb(now, opened, then=0.60), "yes", opened, now)
    assert 0.4 < mid["pos"] < 0.6  # pulled back to the middle of its range


def test_unit_only_the_market_so_far_counts():
    now = time.time()
    opened = now - 500.0
    rows = [(opened - 300.0 + k, 0.95) for k in range(300)] + climb(now, opened)  # quotes from before the open, far above the range
    assert variants.quote_wave_features(rows, "yes", opened, now)["pos"] > 0.9


def test_unit_no_reading_from_a_young_stale_or_still_market():
    now = time.time()
    assert variants.quote_wave_features(climb(now, now - 120.0), "yes", now - 120.0, now) is None  # two minutes of quotes
    opened = now - 500.0
    assert variants.quote_wave_features(climb(now - 30.0, opened), "yes", opened, now) is None  # the newest quote is 30 s old
    assert variants.quote_wave_features([(opened + k, 0.6) for k in range(501)], "yes", opened, now) is None  # the mid never moved
    assert variants.quote_wave_features([], "yes", opened, now) is None


def test_integration_a_buy_at_the_top_of_the_sides_range_is_skipped(tmp_path, monkeypatch):
    now = time.time()
    seed(climb(now, now + LEFT - 900.0))
    out = decide(tmp_path, monkeypatch, now, "wave_quote=true\nwave_pos_max=0.8")
    assert out.action == "pass" and "[wave amp" in out.note and "waits for the low" in out.note


def test_integration_a_buy_lower_in_the_range_goes_in_with_its_reading(tmp_path, monkeypatch):
    now = time.time()
    seed(climb(now, now + LEFT - 900.0, then=0.60))
    out = decide(tmp_path, monkeypatch, now, "wave_quote=true\nwave_pos_max=0.8")
    assert out.action == "bet" and "[wave amp" in out.note


def test_integration_the_rule_reads_the_side(tmp_path, monkeypatch):
    now = time.time()
    seed(climb(now, now + LEFT - 900.0))  # YES at its high is NO at its low
    out = decide(tmp_path, monkeypatch, now, "wave_quote=true\nwave_pos_max=0.8", side="no")
    assert out.action == "bet" and "[wave amp" in out.note


def test_integration_with_no_reading_the_buy_goes_in_and_the_note_says_so(tmp_path, monkeypatch):
    now = time.time()
    ticks.mids.clear()
    out = decide(tmp_path, monkeypatch, now, "wave_quote=true\nwave_pos_max=0.8")
    assert out.action == "bet" and "[wave unreadable" in out.note


def test_integration_without_the_option_the_wave_still_reads_the_spot(tmp_path, monkeypatch):
    now = time.time()
    seed(climb(now, now + LEFT - 900.0, then=0.60))  # a quote reading exists, the spot has no ticks
    out = decide(tmp_path, monkeypatch, now, "wave_pos_max=0.8")
    assert out.action == "pass" and "unreadable for the wave rule" in out.note  # the spot rule sits out with no reading, as before


def test_integration_off_by_default_nothing_is_read(tmp_path, monkeypatch):
    now = time.time()
    seed(climb(now, now + LEFT - 900.0))
    out = decide(tmp_path, monkeypatch, now, "")
    assert out.action == "bet" and "[wave" not in out.note


def test_unit_the_option_changes_a_lanes_version_only_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    base = lanes.LaneSpec(name="x", estimator="market", label="x", price=(0.1, 0.9), hours=(0.0, 1.0), max_spread=0.05, min_volume=0.0)
    assert "wave_quote" not in lanes.settings_of(base, s)
    on = dataclasses.replace(base, wave_quote=True, wave_pos_max=0.8)
    assert lanes.version_of(base, s) != lanes.version_of(on, s)
