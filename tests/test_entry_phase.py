"""The entry-phase gate: skip a buy at the top of a wave (2026-11-15 night, user: "betting when we shouldn't, and on the wrong side ... I would like to see if we can do that too")."""
import math
import time

import pytest

from kalshi_moonshot import config, curverisk, regime, runner, ticks, variants
from kalshi_moonshot.estimators import Estimate
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot


def path(now, run_pct=0.0, last5_pct=0.0, noise_pct=0.01, minutes=90):
    """A perp in minute ticks for `minutes` minutes ending at `now`: a +-noise_pct zig-zag, a steady `run_pct` percent climb over the 13 minutes before the last 5 and `last5_pct` over the last 5 minutes (negative = falling)."""
    out = []
    lvl = 0.0
    for k in range(minutes, -1, -1):  # k minutes ago
        step = noise_pct * (1 if k % 2 == 0 else -1)
        if 5 < k <= 18:
            step += run_pct / 13.0
        elif 0 < k <= 5:
            step += last5_pct / 5.0
        lvl += step
        out.append((now - 60.0 * k, 100.0 * math.exp(lvl / 100.0)))
    return out


def seed(now, **kw):
    ticks.history.clear()
    for ser in variants.BROAD_SERIES:
        for ts, v in path(now, **kw):
            ticks.history.note(variants.crypto_index(ser), ts, v)
    from kalshi_moonshot.watcher import perp_index

    for ts, v in path(now, **kw):
        ticks.history.note(perp_index("KXBTC15M"), ts, v)


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.history.clear()
    variants._broad_memo = (0, None)


def test_unit_side_slopes_are_side_signed_and_in_each_scales_own_units():
    now = time.time()
    p = path(now, run_pct=0.3)
    ts, vs = [x[0] for x in p], [x[1] for x in p]
    up = curverisk.side_slopes(ts, vs, now, "yes")
    down = curverisk.side_slopes(ts, vs, now, "no")
    assert up[13] > 1.5 and up[13] == pytest.approx(-down[13])
    quiet = path(now)
    q = curverisk.side_slopes([x[0] for x in quiet], [x[1] for x in quiet], now, "yes")
    assert abs(q[13]) < 1.0
    assert curverisk.side_slopes(ts[-10:], vs[-10:], now, "yes") is None  # not a full hour of ticks
    flat = [100.0] * len(ts)
    assert curverisk.side_slopes(ts, flat, now, "yes") is None  # a flat hour has no unit


LANE = "[lane.ph]\nestimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\n{extra}\n"


class Fixed:
    def estimate(self, client, settings, shot):
        return Estimate(0.9, "fixed")


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


def test_integration_a_side_that_has_already_run_far_is_not_bought_and_the_note_says_so(tmp_path, monkeypatch):
    now = time.time()
    seed(now, run_pct=0.5)
    out = decide(tmp_path, monkeypatch, now, "entry_run_max=1.5")
    assert out.action == "pass" and "already run" in out.note and "[phase z13" in out.note


def test_integration_the_same_run_is_a_run_the_other_way_for_the_other_side(tmp_path, monkeypatch):
    now = time.time()
    seed(now, run_pct=0.5)  # the perp rose: a NO bet has run against it, z13 negative
    assert decide(tmp_path, monkeypatch, now, "entry_run_max=1.5", side="no").action == "bet"


def test_integration_a_quiet_hour_passes_both_gates(tmp_path, monkeypatch):
    now = time.time()
    seed(now)
    out = decide(tmp_path, monkeypatch, now, "entry_run_max=1.5\nentry_rotation_min=-0.3")
    assert out.action == "bet" and "[phase z13" in out.note


def test_integration_a_short_move_rolled_over_against_the_long_one_is_not_bought(tmp_path, monkeypatch):
    now = time.time()
    seed(now, run_pct=0.4, last5_pct=-0.5)  # up for a while, then the last five minutes fall back: z5 negative, z21 positive
    out = decide(tmp_path, monkeypatch, now, "entry_rotation_min=-0.3")
    assert out.action == "pass" and "rolled over" in out.note


def test_integration_with_the_options_off_nothing_changes_and_nothing_is_read(tmp_path, monkeypatch):
    now = time.time()
    seed(now, run_pct=0.5)
    out = decide(tmp_path, monkeypatch, now, "")
    assert out.action == "bet" and "[phase" not in out.note


def test_integration_with_no_perp_hour_the_gate_does_not_block_and_says_it_could_not_read(tmp_path, monkeypatch):
    now = time.time()
    ticks.history.clear()
    out = decide(tmp_path, monkeypatch, now, "entry_run_max=1.5")
    assert out.action == "bet" and "phase unreadable" in out.note


def test_unit_the_options_change_a_lanes_version_only_when_set(tmp_path, monkeypatch):
    import dataclasses

    from kalshi_moonshot import lanes

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    base = lanes.LaneSpec(name="x", estimator="market", label="x", price=(0.1, 0.9), hours=(0.0, 1.0), max_spread=0.05, min_volume=0.0)
    assert "entry_run_max" not in lanes.settings_of(base, s) and "entry_rotation_min" not in lanes.settings_of(base, s)
    on = dataclasses.replace(base, entry_run_max=1.5, entry_rotation_min=-0.3)
    assert lanes.settings_of(on, s)["entry_run_max"] == "1.5" and lanes.version_of(base, s) != lanes.version_of(on, s)
