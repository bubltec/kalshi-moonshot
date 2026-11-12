"""The per-tick curve gap buy (tickgap.py, lane field tick_buy): warm-up, the side's own cutoff, persistence, anomalies."""

import json
from types import SimpleNamespace

import pytest

from kalshi_moonshot import curvevalue, tickgap


def spec(**kw):
    base = dict(tick_buy_hours=6.0, tick_buy_quantile=0.8, tick_buy_persist=5.0, spot_source="kalshi")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def env(tmp_path, monkeypatch):
    tickgap.reset()
    s = SimpleNamespace(data_dir=tmp_path)
    feed = {"gap": 0.0, "rate": 1e-4}

    def fake_sample(series, side, left, ask, now, strike, source="kalshi"):
        return curvevalue.Curve(0.8, 0.001, feed["rate"], feed["rate"]), feed["gap"], (60, 0.5, 1.0)

    monkeypatch.setattr(tickgap, "sample", fake_sample)
    yield s, feed
    tickgap.reset()


def feed_history(s, feed, n=300, t0=1_000_000.0):
    """n readings with gaps spread evenly from -0.10 to +0.10, so the 80th percentile is about +0.06 and the 1st and 99th about -0.10 and +0.10."""
    for i in range(n):
        feed["gap"] = -0.10 + 0.20 * i / (n - 1)
        tickgap.decide(s, "KXGOLD15M", f"M{i}", "yes", 400.0, 0.80, None, spec(), now=t0 + i * 5.0)
    tickgap._fit.clear()  # the cutoffs are refitted hourly; the test wants them from the whole history
    return t0 + n * 5.0


def test_no_buy_until_the_side_has_a_history(env):
    s, feed = env
    feed["gap"] = 0.5
    v = tickgap.decide(s, "KXGOLD15M", "M1", "yes", 400.0, 0.80, None, spec(), now=1_000_000.0)
    assert not v.ok and v.status == "warm-up"


def test_below_the_sides_own_cutoff_is_not_a_buy(env):
    s, feed = env
    t = feed_history(s, feed)
    feed["gap"] = 0.0
    v = tickgap.decide(s, "KXGOLD15M", "NEW", "yes", 400.0, 0.80, None, spec(), now=t)
    assert not v.ok and v.status == "below"


def test_a_cleared_gap_buys_after_it_has_held_for_the_persistence(env):
    s, feed = env
    t = feed_history(s, feed)
    feed["gap"] = 0.07  # above the 80th percentile (about +0.06), inside the 99th
    first = tickgap.decide(s, "KXGOLD15M", "NEW", "yes", 400.0, 0.80, None, spec(), now=t)
    assert not first.ok and first.status == "waiting"
    second = tickgap.decide(s, "KXGOLD15M", "NEW", "yes", 395.0, 0.80, None, spec(), now=t + 5.0)
    assert second.ok and second.status == "clean" and "held 5s" in second.note


def test_a_break_in_the_run_starts_the_wait_again(env):
    s, feed = env
    t = feed_history(s, feed)
    feed["gap"] = 0.07
    tickgap.decide(s, "KXGOLD15M", "NEW", "yes", 400.0, 0.80, None, spec(), now=t)
    feed["gap"] = 0.0
    tickgap.decide(s, "KXGOLD15M", "NEW", "yes", 398.0, 0.80, None, spec(), now=t + 2.0)
    feed["gap"] = 0.07
    again = tickgap.decide(s, "KXGOLD15M", "NEW", "yes", 395.0, 0.80, None, spec(), now=t + 5.0)
    assert not again.ok and again.status == "waiting"


def test_an_extreme_gap_is_logged_and_not_bought(env):
    s, feed = env
    t = feed_history(s, feed)
    feed["gap"] = 0.30  # far above the side's own 99th percentile
    tickgap.decide(s, "KXGOLD15M", "NEW", "yes", 400.0, 0.80, None, spec(), now=t)
    v = tickgap.decide(s, "KXGOLD15M", "NEW", "yes", 395.0, 0.80, None, spec(), now=t + 5.0)
    assert not v.ok and v.status == "extreme_gap"
    rows = [json.loads(x) for x in (s.data_dir / "tickgap_anomalies.jsonl").read_text().splitlines()]
    assert rows and rows[-1]["status"] == "extreme_gap" and rows[-1]["ticker"] == "NEW" and rows[-1]["gap"] == pytest.approx(0.30)


def test_a_wild_curve_rate_is_an_anomaly_even_with_an_ordinary_gap(env):
    s, feed = env
    t = feed_history(s, feed)
    feed["gap"] = 0.07
    feed["rate"] = 1e-3  # ten times the side's own median
    v = tickgap.decide(s, "KXGOLD15M", "NEW", "yes", 400.0, 0.80, None, spec(), now=t)
    assert not v.ok and v.status == "high_rate"


def test_the_history_survives_a_restart(env):
    s, feed = env
    t = feed_history(s, feed, n=300)
    tickgap.decide(s, "KXGOLD15M", "X", "yes", 400.0, 0.80, None, spec(), now=t + 700.0)  # past the ten-minute save
    assert (s.data_dir / "tickgap_history.json").exists()
    tickgap._hist.clear(); tickgap._fit.clear(); tickgap._state.update(loaded=False, saved=0.0)
    feed["gap"] = 0.07
    v = tickgap.decide(s, "KXGOLD15M", "Y", "yes", 400.0, 0.80, None, spec(), now=t + 705.0)
    assert v.status != "warm-up"


def test_a_resting_bid_must_still_clear_the_cutoff(env):
    s, feed = env
    t = feed_history(s, feed)
    feed["gap"] = 0.07
    assert tickgap.recheck("KXGOLD15M", "yes", 390.0, 0.80, None, spec(), now=t)
    feed["gap"] = 0.01
    assert not tickgap.recheck("KXGOLD15M", "yes", 390.0, 0.80, None, spec(), now=t)
