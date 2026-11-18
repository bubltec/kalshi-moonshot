"""The risk score (riskscore.py; lane fields risk_score_buy, risk_score_edge, curve_risk_score): 2026-11-16, user: "all these factors need to build towards that risk score"; "I thought you were putting our new logic up as a lane"."""
import dataclasses
import json
import math
import time

import pytest

from kalshi_moonshot import config, lanes, regime, riskscore, runner, ticks, variants
from kalshi_moonshot.estimators import Estimate
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot

LANE = "[lane.rs]\nestimator='market'\nprice=[0.05,0.97]\nhours=[0.0,1.0]\nalways=true\nfixed_stake=1.0\nseries=['KXBTC15M']\n{extra}\n"


class Fixed:
    def estimate(self, client, settings, shot):
        return Estimate(0.9, "fixed")


def index(opened, now, start=100.0, lift=0.0, step=5):
    """(times, values): the index wiggles around `start` for 70 minutes before the open, then sits `lift` (a fraction) over it for the market so far."""
    ts, vs = [], []
    t = opened - 4200.0
    k = 0
    while t <= now:
        base = start * (1.0 + (lift if t > opened + 30.0 else 0.0))
        ts.append(t)
        vs.append(base * (1.0 + 0.0004 * math.sin(k / 7.0)))
        t += step
        k += 1
    return ts, vs


def mids(opened, now, level=0.80, flip_at=None):
    """(time, YES mid) rows one a second: at `level` with a small wobble; before `flip_at` seconds in (if given) at 1 - level."""
    return [(opened + k, (1.0 - level if flip_at and k < flip_at else level) + (0.004 if k % 2 else -0.004)) for k in range(int(now - opened) + 1)]


def test_unit_a_wide_cushion_and_a_high_price_read_as_low_risk_and_the_two_sides_add_to_one():
    now = 1_000_000.0
    opened = now - 480.0
    ts, vs = index(opened, now, lift=0.004)
    x = riskscore.features(ts, vs, now, opened, 100.0, mids(opened, now), [])
    assert x is not None and len(x) == len(riskscore.NAMES)
    yes, no = riskscore.chance(x, "yes"), riskscore.chance(x, "no")
    assert yes > 0.75 and abs(yes + no - 1.0) < 1e-9


def test_unit_an_even_market_on_the_line_is_never_low_risk():
    now = 1_000_000.0
    opened = now - 480.0
    ts, vs = index(opened, now)
    got = riskscore.held(ts, vs, now, opened, 100.0, mids(opened, now, level=0.50), [], "yes")
    assert got is not None and all(0.35 < c < 0.65 for c in got)
    assert not riskscore.low_risk(got, 0.25)


def test_unit_nothing_after_the_moment_is_read():
    now = 1_000_000.0
    opened = now - 480.0
    ts, vs = index(opened, now + 120.0, lift=0.004)
    m = mids(opened, now + 120.0)
    late_ts, late_vs = ts + [now + 121.0], vs + [50.0]  # a crash after the moment
    a = riskscore.features(ts[:len([t for t in ts if t <= now])], vs[:len([t for t in ts if t <= now])], now, opened, 100.0, [r for r in m if r[0] <= now], [])
    b = riskscore.features(late_ts, late_vs, now, opened, 100.0, m + [(now + 121.0, 0.05)], [])
    assert a == b


def test_unit_low_risk_has_to_have_held_at_every_look():
    now = 1_000_000.0
    opened = now - 480.0
    ts, vs = index(opened, now, lift=0.004)
    steady = riskscore.held(ts, vs, now, opened, 100.0, mids(opened, now), [], "yes")
    assert riskscore.low_risk(steady, 0.25)
    fresh = riskscore.held(ts, vs, now, opened, 100.0, mids(opened, now, flip_at=440), [], "yes")  # YES only took the lead 40 s ago
    assert fresh is not None and 1.0 - fresh[0] <= 0.25 and not riskscore.low_risk(fresh, 0.25)
    assert not riskscore.low_risk(None, 0.25)


def test_unit_no_reading_from_a_young_market_a_stale_index_or_a_price_at_the_edge():
    now = 1_000_000.0
    opened = now - 480.0
    ts, vs = index(opened, now, lift=0.004)
    assert riskscore.features(ts, vs, opened + 30.0, opened, 100.0, mids(opened, now), []) is None
    assert riskscore.features([t for t in ts if t <= now - 60.0], vs[:len([t for t in ts if t <= now - 60.0])], now, opened, 100.0, mids(opened, now), []) is None
    assert riskscore.features(ts, vs, now, opened, 100.0, mids(opened, now, level=0.985), []) is None


def test_unit_the_weights_file_replaces_the_built_in_fit(tmp_path):
    assert riskscore.weights(None) == riskscore.WEIGHTS
    assert riskscore.weights(str(tmp_path)) == riskscore.WEIGHTS  # no file
    w = {n: 0.0 for n in riskscore.NAMES} | {"price": 1.0}
    (tmp_path / "risk_score.json").write_text(json.dumps({"weights": w}))
    riskscore._cache["checked"] = -1e9  # the file is looked at once every 30 s: look now
    got = riskscore.weights(str(tmp_path))
    assert got[0] == 1.0 and sum(abs(v) for v in got[1:]) == 0.0


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.mids.clear()
    ticks.history.clear()
    riskscore._prev.clear()


def decide(tmp_path, monkeypatch, now, extra, level=0.80, lift=0.004, flip_at=None, ask=0.81, seed=True):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(extra=extra))
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    left = 420.0
    opened = now + left - 900.0
    ticks.mids.clear()
    ticks.history.clear()
    riskscore._prev.clear()
    if seed:
        for ts, v in zip(*index(opened, now, lift=lift)):
            ticks.history.note(variants.crypto_index("KXBTC15M"), ts, v)
        for ts, mid in mids(opened, now, level=level, flip_at=flip_at):
            ticks.mids.note("KXBTC15M-7", ts, mid - 0.005, mid + 0.005)
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + left, 100.0, now))
    store.db.commit()
    shot = LongShot("KXBTC15M-7", "t", "", ask - 0.01, ask, left / 3600.0, "2099-01-01T00:00:00Z", left / 3600.0, lane="rs", series="KXBTC15M", side="yes")
    return runner.evaluate(s, None, None, store, shot, estimators={"market": Fixed()})


def test_integration_a_low_risk_that_has_held_is_bought_and_the_note_carries_the_number(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, time.time(), "risk_score_buy=0.25\nrisk_score_edge=-0.05")
    assert out.action == "bet" and "[risk score: risk " in out.note and "by Kalshi's price" in out.note


def test_integration_a_coin_flip_is_not_bought(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, time.time(), "risk_score_buy=0.25\nrisk_score_edge=-0.05", level=0.50, lift=0.0, ask=0.51)
    assert out.action == "pass" and "that has held for a minute" in out.note


def test_integration_a_side_that_only_just_took_the_lead_is_not_bought(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, time.time(), "risk_score_buy=0.25\nrisk_score_edge=-0.05", flip_at=440)
    assert out.action == "pass" and "that has held for a minute" in out.note


def test_integration_a_chance_that_does_not_clear_the_costs_is_not_bought(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, time.time(), "risk_score_buy=0.25\nrisk_score_edge=0.0", ask=0.95)
    assert out.action == "pass" and "does not clear the ask and its fee" in out.note


def test_integration_with_no_reading_the_lane_does_not_buy(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, time.time(), "risk_score_buy=0.25", seed=False)
    assert out.action == "pass" and "[risk score unreadable]" in out.note


def test_integration_off_by_default_nothing_is_read(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, time.time(), "")
    assert out.action == "bet" and "risk score" not in out.note


def test_unit_the_fields_change_a_lanes_version_only_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    base = lanes.LaneSpec(name="x", estimator="market", label="x", price=(0.1, 0.9), hours=(0.0, 1.0), max_spread=0.05, min_volume=0.0)
    out = lanes.settings_of(base, s)
    assert "risk_score_buy" not in out and "curve_risk_score" not in out
    assert lanes.version_of(base, s) != lanes.version_of(dataclasses.replace(base, risk_score_buy=0.25), s)
    assert lanes.version_of(base, s) != lanes.version_of(dataclasses.replace(base, curve_risk_score=True), s)


def test_unit_the_last_four_markets_come_from_memory_alone():
    now = 1_000_000.0
    opened = now - 480.0
    ts, vs = index(opened, now, lift=0.004)
    unit = riskscore.unit_before(ts, vs, opened)
    series_mids = [(opened - 900.0 + k, 0.30 if k < 400 else 0.70) for k in range(900)]  # the market before this one: NO led, then YES
    prev = riskscore.previous(ts, vs, series_mids, opened, unit)
    assert len(prev) == 4 and prev[0][1] == 1 and prev[1][1] is None  # one flip in the last market; no mids held for the one before
    assert prev[0][0] is not None and abs(prev[0][0]) < 1.0  # it finished near its target: the index was flat before this open


def test_integration_the_reading_makes_no_database_read(tmp_path, monkeypatch):
    now = time.time()
    seen = []
    real = Store.__init__

    def spy(self, *a, **k):
        real(self, *a, **k)
        inner = self.db

        class Watch:
            def execute(_, sql, *args):
                seen.append(sql)
                return inner.execute(sql, *args)

            def __getattr__(_, name):
                return getattr(inner, name)

        self.db = Watch()

    monkeypatch.setattr(Store, "__init__", spy)
    out = decide(tmp_path, monkeypatch, now, "risk_score_buy=0.25\nrisk_score_edge=-0.05\nentry_scenario_view=true")
    assert out.action == "bet" and "[risk score: risk " in out.note[:60]
    assert not [q for q in seen if "settled_value" in q]  # the last four markets are no longer asked of the database


def test_unit_a_side_bought_cheap_is_not_on_the_sale_line_from_the_start():
    assert riskscore.sale_line(0.25, 0.80) == 0.25
    assert riskscore.sale_line(0.25, 0.24) == 0.12
    assert riskscore.sale_line(0.25, 0.0) == 0.25
