"""The 3-hour spike reading in the buy's note (lane field entry_spike_view), shadow: 2026-11-16, user, on the faded spikes that never came back: "What did their historical view look like? How much of a spike was it? Does the 3 hour view show similar spikes?"; "Yes, log it"."""
import dataclasses
import math
import time

import pytest

from kalshi_moonshot import config, curverisk, lanes, regime, runner, ticks, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot

from tests.test_entry_phase import Fixed, LANE, seed  # noqa: F401  (the same lane file and perp seeding)


def perp(now, jump_pct=0.0, noise_pct=0.01, minutes=200):
    """A perp ticking every 30 s for `minutes` minutes ending at `now`: a +-noise_pct zig-zag, and `jump_pct` percent added over the last 90 seconds."""
    out, lvl = [], 0.0
    n = minutes * 2
    for k in range(n, -1, -1):  # k half-minutes ago
        lvl += noise_pct * (1 if k % 2 == 0 else -1)
        if k < 3:
            lvl += jump_pct / 3.0
        out.append((now - 30.0 * k, 100.0 * math.exp(lvl / 100.0)))
    return out


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.history.clear()
    variants._broad_memo = (0, None)


def read(now, side="yes", **kw):
    p = perp(now, **kw)
    return curverisk.spike_view([x[0] for x in p], [x[1] for x in p], now, side)


def test_unit_a_quiet_stretch_is_an_ordinary_move():
    z, similar = read(time.time())
    assert abs(z) < 1.5 and similar > 60  # most of the 120 stretches moved as far


def test_unit_a_jump_is_large_and_has_no_like_in_three_hours():
    z, similar = read(time.time(), jump_pct=0.3)
    assert z > 3 and similar == 0


def test_unit_the_reading_is_signed_toward_the_bet_side():
    now = time.time()
    up, down = read(now, jump_pct=0.3), read(now, side="no", jump_pct=0.3)
    assert up[0] == pytest.approx(-down[0]) and up[1] == down[1]


def test_unit_no_reading_without_three_hours_of_ticks_or_with_a_flat_perp():
    now = time.time()
    assert read(now, minutes=100) is None  # under three hours
    flat = perp(now)
    assert curverisk.spike_view([x[0] for x in flat], [100.0] * len(flat), now, "yes") is None
    assert curverisk.spike_view([], [], now, "yes") is None


def decide(tmp_path, monkeypatch, now, extra, side="yes"):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(extra=extra))
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    shot = LongShot("KXBTC15M-7", "t", "", 0.88, 0.89, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="ph", series="KXBTC15M", side=side)
    return runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})


def test_integration_the_buy_note_carries_the_reading_and_the_bet_still_goes_in(tmp_path, monkeypatch):
    now = time.time()
    seed(now, minutes=300)
    out = decide(tmp_path, monkeypatch, now, "entry_spike_view=true")
    assert out.action == "bet" and "[spike view z " in out.note and "of 120 as large]" in out.note


def test_integration_a_pass_carries_the_reading_too(tmp_path, monkeypatch):
    now = time.time()
    seed(now, run_pct=0.5, minutes=300)  # the side has run: the entry-phase gate passes on it
    out = decide(tmp_path, monkeypatch, now, "entry_spike_view=true\nentry_run_max=0.1")
    assert out.action == "pass" and "[spike view " in out.note


def test_integration_with_no_perp_the_note_says_unreadable_and_the_buy_goes_in(tmp_path, monkeypatch):
    now = time.time()
    ticks.history.clear()
    out = decide(tmp_path, monkeypatch, now, "entry_spike_view=true")
    assert out.action == "bet" and "[spike view unreadable]" in out.note


def test_integration_off_by_default_nothing_is_read(tmp_path, monkeypatch):
    now = time.time()
    seed(now, minutes=300)
    assert "[spike view" not in decide(tmp_path, monkeypatch, now, "").note


def test_unit_the_field_changes_a_lanes_version_only_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    base = lanes.LaneSpec(name="x", estimator="market", label="x", price=(0.1, 0.9), hours=(0.0, 1.0), max_spread=0.05, min_volume=0.0)
    assert "entry_spike_view" not in lanes.settings_of(base, s)
    assert lanes.version_of(base, s) != lanes.version_of(dataclasses.replace(base, entry_spike_view=True), s)
