"""The flip rule (lane field flip_skip_minutes): 2026-11-16, user, on DOGE 09:30 YES bought at the top of an 80-second spike and stopped out three minutes later: "It bought the wrong side tho ... the history shows a likely outcome"; studies/results/2026-11-16-spike-side.txt."""
import dataclasses
import time

import pytest

from kalshi_moonshot import config, lanes, regime, runner, ticks, variants
from kalshi_moonshot.estimators import Estimate
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot

LANE = "[lane.fl]\nestimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\n{extra}\n"


class Fixed:
    def estimate(self, client, settings, shot):
        return Estimate(0.9, "fixed")


def spike(now, opened, base=0.40, top=0.62, last=60):
    """(time, YES mid) rows, one a second from the open: YES sits at `base` all market, then jumps to `top` over the last `last` seconds."""
    n = int(now - opened)
    return [(opened + k, base + (0.004 if k % 2 else -0.004) if k <= n - last else base + (top - base) * (k - (n - last)) / last) for k in range(n + 1)]


def seed(rows, ticker="KXBTC15M-7"):
    ticks.mids.clear()
    for ts, mid in rows:
        ticks.mids.note(ticker, ts, mid - 0.005, mid + 0.005)


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.mids.clear()
    ticks.history.clear()


def decide(tmp_path, monkeypatch, now, extra, side="yes", left=600.0):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(extra=extra))
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + left, 100.0, now))
    store.db.commit()
    shot = LongShot("KXBTC15M-7", "t", "", 0.61, 0.62, left / 3600.0, "2099-01-01T00:00:00Z", left / 3600.0, lane="fl", series="KXBTC15M", side=side)
    return runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})


def test_unit_the_share_is_the_time_the_side_was_the_favorite_since_the_open():
    now = time.time()
    opened = now - 300.0
    rows = spike(now, opened)
    assert variants.quote_led_share(rows, "yes", opened, now) < 0.2  # YES has only just become the favorite
    assert variants.quote_led_share(rows, "no", opened, now) > 0.8  # NO led the market
    steady = [(opened + k, 0.62) for k in range(301)]
    assert variants.quote_led_share(steady, "yes", opened, now) == 1.0


def test_unit_only_the_market_so_far_counts():
    now = time.time()
    opened = now - 300.0
    rows = [(opened - 200.0 + k, 0.90) for k in range(200)] + spike(now, opened)  # YES was high before the open: not this market
    assert variants.quote_led_share(rows, "yes", opened, now) < 0.2


def test_unit_no_reading_from_a_young_or_stale_market():
    now = time.time()
    assert variants.quote_led_share(spike(now, now - 60.0, last=10), "yes", now - 60.0, now) is None  # a minute of quotes
    opened = now - 300.0
    assert variants.quote_led_share(spike(now - 30.0, opened), "yes", opened, now) is None  # the newest quote is 30 s old
    assert variants.quote_led_share([], "yes", opened, now) is None


def test_integration_a_new_favorite_early_in_the_market_is_skipped(tmp_path, monkeypatch):
    now = time.time()
    seed(spike(now, now - 300.0))  # 5 minutes in, YES jumped from 0.40 to 0.62 in the last minute
    out = decide(tmp_path, monkeypatch, now, "flip_skip_minutes=9.0")
    assert out.action == "pass" and "[flip: led 0." in out.note and "skips a new favorite before minute 9" in out.note


def test_integration_the_side_that_led_the_market_is_not_a_flip(tmp_path, monkeypatch):
    now = time.time()
    seed([(now - 300.0 + k, 0.62 + (0.004 if k % 2 else -0.004)) for k in range(301)])  # YES the favorite all market
    out = decide(tmp_path, monkeypatch, now, "flip_skip_minutes=9.0")
    assert out.action == "bet" and "[led 1.00]" in out.note


def test_integration_from_minute_nine_a_flip_is_bought(tmp_path, monkeypatch):
    now = time.time()
    seed(spike(now, now - 600.0))  # 10 minutes in: a late flip holds
    out = decide(tmp_path, monkeypatch, now, "flip_skip_minutes=9.0", left=300.0)
    assert out.action == "bet" and "[led 0." in out.note


def test_integration_a_side_that_shared_the_lead_is_bought(tmp_path, monkeypatch):
    now = time.time()
    opened = now - 300.0
    seed([(opened + k, (0.40 if k < 150 else 0.62) + (0.004 if k % 2 else -0.004)) for k in range(301)])  # YES led the second half: the other side led 50%, under the 80%
    out = decide(tmp_path, monkeypatch, now, "flip_skip_minutes=9.0")
    assert out.action == "bet" and "[led 0.5" in out.note


def test_integration_with_no_reading_the_buy_goes_in_and_the_note_says_so(tmp_path, monkeypatch):
    now = time.time()
    ticks.mids.clear()
    out = decide(tmp_path, monkeypatch, now, "flip_skip_minutes=9.0")
    assert out.action == "bet" and "[flip unreadable" in out.note


def test_integration_off_by_default_nothing_is_read(tmp_path, monkeypatch):
    now = time.time()
    seed(spike(now, now - 300.0))
    out = decide(tmp_path, monkeypatch, now, "")
    assert out.action == "bet" and "flip" not in out.note and "[led" not in out.note


def test_unit_the_rule_changes_a_lanes_version_only_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    base = lanes.LaneSpec(name="x", estimator="market", label="x", price=(0.1, 0.9), hours=(0.0, 1.0), max_spread=0.05, min_volume=0.0)
    assert "flip_skip_minutes" not in lanes.settings_of(base, s)
    on = dataclasses.replace(base, flip_skip_minutes=9.0)
    assert lanes.version_of(base, s) != lanes.version_of(on, s)


# The trailing-side buy (2026-11-16, user: "ok sounds good what you suggested"): lane fields entry_led_max and wave_pos_min. studies/results/2026-11-16-buy-state-table.txt.
def comeback(now, opened, base=0.25, low=0.15, top=0.45, pull=None):
    """(time, YES mid) rows: YES trails at `base`, dips to `low` mid-market, then climbs to `top`; `pull` sets where it ends (None = at `top`)."""
    n = int(now - opened)
    end = top if pull is None else pull
    rows = []
    for k in range(n + 1):
        f = k / n
        mid = base if f < 0.3 else low if f < 0.5 else low + (top - low) * (f - 0.5) / 0.4 if f < 0.9 else top + (end - top) * (f - 0.9) / 0.1
        rows.append((opened + k, mid + (0.002 if k % 2 else -0.002)))
    return rows


TRAIL = "entry_led_max=0.2\nwave_quote=true\nwave_pos_min=0.2\nwave_pos_max=0.8"


def test_integration_the_trailing_side_off_its_low_is_bought(tmp_path, monkeypatch):
    now = time.time()
    seed(comeback(now, now - 600.0, pull=0.33))  # trailed all market, off its low, not at its high
    out = decide(tmp_path, monkeypatch, now, TRAIL, left=300.0)
    assert out.action == "bet" and "[led 0.00]" in out.note and "[wave amp" in out.note


def test_integration_the_side_that_led_is_not_bought(tmp_path, monkeypatch):
    now = time.time()
    seed(comeback(now, now - 600.0, pull=0.33))  # the same market's NO led it
    out = decide(tmp_path, monkeypatch, now, TRAIL, side="no", left=300.0)
    assert out.action == "pass" and "the lane buys a side that led 20% or less" in out.note


def test_integration_the_trailing_side_at_its_low_or_its_high_is_not_bought(tmp_path, monkeypatch):
    now = time.time()
    seed(comeback(now, now - 600.0))  # ends at the top of its climb
    out = decide(tmp_path, monkeypatch, now, TRAIL, left=300.0)
    assert out.action == "pass" and "waits for the low" in out.note
    seed(comeback(now, now - 600.0, pull=0.16))  # fell back to its low
    out = decide(tmp_path, monkeypatch, now, TRAIL, left=300.0)
    assert out.action == "pass" and "still at its low" in out.note


def test_integration_with_no_reading_the_trailing_lane_does_not_buy(tmp_path, monkeypatch):
    now = time.time()
    ticks.mids.clear()
    out = decide(tmp_path, monkeypatch, now, TRAIL, left=300.0)
    assert out.action == "pass" and "[led unreadable" in out.note


def test_unit_the_trailing_settings_change_a_lanes_version_only_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    base = lanes.LaneSpec(name="x", estimator="market", label="x", price=(0.1, 0.9), hours=(0.0, 1.0), max_spread=0.05, min_volume=0.0)
    out = lanes.settings_of(base, s)
    assert "entry_led_max" not in out and "wave_pos_min" not in out
    assert lanes.version_of(base, s) != lanes.version_of(dataclasses.replace(base, entry_led_max=0.2), s)
    assert lanes.version_of(base, s) != lanes.version_of(dataclasses.replace(base, wave_pos_min=0.2), s)
