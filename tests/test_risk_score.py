"""The risk score (riskscore.py; lane fields risk_score_buy, risk_score_edge, curve_risk_score): 2026-11-16, user: "all these factors need to build towards that risk score"; "I thought you were putting our new logic up as a lane"."""
import json
import math
import time

import pytest

from kalshi_moonshot import config, regime, riskscore, runner, ticks, variants
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
    fresh = riskscore.held(ts, vs, now, opened, 100.0, mids(opened, now, flip_at=460), [], "yes")  # YES only took the lead 20 s ago, inside the 28 s look back for 420 s left
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


def test_integration_off_by_default_nothing_is_read(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, time.time(), "")
    assert out.action == "bet" and "risk score" not in out.note


def test_unit_the_last_four_markets_come_from_memory_alone():
    now = 1_000_000.0
    opened = now - 480.0
    ts, vs = index(opened, now, lift=0.004)
    unit = riskscore.unit_before(ts, vs, opened)
    series_mids = [(opened - 900.0 + k, 0.30 if k < 400 else 0.70) for k in range(900)]  # the market before this one: NO led, then YES
    prev = riskscore.previous(ts, vs, series_mids, opened, unit)
    assert len(prev) == 4 and prev[0][1] == 1 and prev[1][1] is None  # one flip in the last market; no mids held for the one before
    assert prev[0][0] is not None and abs(prev[0][0]) < 1.0  # it finished near its target: the index was flat before this open


# The feed (2026-11-18, user: "why isn't it reading kalshi?"; "Yes, fix it"): the score reads Kalshi's own price for the underlying. A commodity's readings had come from the outside feed since 11-17.
def gold(now, perp_lift, feed_lift, perp_until=None):
    """A gold market 420 s from its close, with the Kalshi perp reference and the outside feed telling different stories; the perp's ticks stop at `perp_until` when given."""
    from kalshi_moonshot import perpref

    opened = now - 480.0
    ticks.mids.clear()
    ticks.history.clear()
    riskscore._prev.clear()
    for ts, v in zip(*index(opened, now, lift=perp_lift)):
        if perp_until is None or ts <= perp_until:
            ticks.history.note(perpref.index_for_series("KXGOLD15M"), ts, v)
    for ts, v in zip(*index(opened, now, lift=feed_lift)):
        ticks.history.note(variants.biquote_index("KXGOLD15M"), ts, v)
    for ts, mid in mids(opened, now, level=0.80):
        ticks.mids.note("KXGOLD15M-7", ts, mid - 0.005, mid + 0.005)
    return opened


def cushion_of(now, opened):
    got = riskscore.live("KXGOLD15M", "KXGOLD15M-7", "yes", opened + 900.0, now, whole=True)
    assert got is not None
    return got[3][riskscore.NAMES.index("cushion")]


def test_integration_a_commoditys_cushion_is_read_from_the_kalshi_perp_not_the_outside_feed():
    now = time.time()
    up = cushion_of(now, gold(now, perp_lift=0.004, feed_lift=-0.004))  # the perp is over its open, the outside feed under its own
    down = cushion_of(now, gold(now, perp_lift=-0.004, feed_lift=0.004))
    assert up > 0 > down  # the reading follows the perp both times


def test_integration_while_the_perp_is_silent_the_outside_feed_is_the_fallback():
    now = time.time()
    opened = gold(now, perp_lift=0.004, feed_lift=-0.004, perp_until=now - 120.0)  # no perp tick for two minutes (variants.PERP_FRESH is 30 s)
    assert cushion_of(now, opened) < 0  # the feed's story, the only live one


def test_unit_a_coin_still_reads_its_own_settlement_index():
    assert variants.spot_index("KXBTC15M", "kalshi", time.time()) == variants.crypto_index("KXBTC15M") == variants.spot_index("KXBTC15M", "", time.time())


def test_unit_the_looks_scale_with_the_time_left_and_stop_shrinking_at_90_seconds():
    assert riskscore.looks_for(900.0) == pytest.approx((0.0, 30.0, 60.0))
    assert riskscore.looks_for(450.0) == pytest.approx((0.0, 15.0, 30.0))
    assert riskscore.looks_for(90.0) == pytest.approx((0.0, 3.0, 6.0))
    assert riskscore.looks_for(20.0) == pytest.approx((0.0, 3.0, 6.0)) and riskscore.looks_for(-5.0) == pytest.approx((0.0, 3.0, 6.0))
