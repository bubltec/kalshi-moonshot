"""The binary risk model (binaryrisk.py, 2026-11-20): the readings, the side's view, the fit, the bar, the never-bet range, and the shadow step that never says no."""
import json
import math
import random
import time

import pytest

from kalshi_moonshot import binaryrisk, buychain, config, ticks
from kalshi_moonshot.strategy import LongShot


def walk(n=4000, seed=1, start=100.0, drift=0.0):
    rnd = random.Random(seed)
    T, V, v = [], [], start
    t0 = 1_000_000.0
    for i in range(n):
        v *= math.exp(drift + rnd.gauss(0, 0.0002))
        T.append(t0 + i)
        V.append(v)
    return T, V


def test_unit_readings_need_an_hour_of_fresh_ticks():
    T, V = walk(4000)
    now = T[-1]
    assert binaryrisk.readings(T[-500:], V[-500:], now, V[-1], 300.0) is None  # under an hour
    assert binaryrisk.readings(T, V, now + 60.0, V[-1], 300.0) is None  # the last tick is stale
    f = binaryrisk.readings(T, V, now, V[-1], 300.0)
    assert f is not None and abs(f["cz"]) < 1e-9 and f["left"] == 5.0  # spot at the strike: no cushion
    assert 0.0 <= f["pos15"] <= 1.0 and 0.0 <= f["h_pos"] <= 1.0 and 0.0 <= f["eff5"] <= 1.0


def test_unit_the_cushion_is_in_typical_moves_and_signed_by_side():
    T, V = walk(4000)
    now = T[-1]
    up = binaryrisk.readings(T, V, now, V[-1] * 0.999, 300.0)  # the strike under the spot: YES is ahead
    assert up["cz"] > 0
    yes = binaryrisk.side_vector(up, "yes", 0.60, 0.0, 0.0)
    no = binaryrisk.side_vector(up, "no", 0.40, 0.0, 0.0)
    i = binaryrisk.NAMES.index("cz")
    assert yes[i] > 0 > no[i]  # the same curve is a lead for YES and a deficit for NO
    j, k = binaryrisk.NAMES.index("bounce_with"), binaryrisk.NAMES.index("bounce_against")
    assert yes[j] == no[k] and yes[k] == no[j]  # the bounce room swaps with the side
    assert abs(yes[binaryrisk.NAMES.index("h_pos")] + no[binaryrisk.NAMES.index("h_pos")] - 1.0) < 1e-9


def test_unit_recent_outcomes_use_only_windows_before_the_moment_and_need_twenty():
    r = binaryrisk.Recent([(float(i), 1 if i % 4 else 0) for i in range(100)])  # a window closes each second, 75% YES
    assert abs(r.yes(100.0, 100.0 / 3600.0) - 0.25) < 1e-9  # all 100 windows before t=100: 0.75 - 0.5
    assert r.yes(10.0, 1.0) == 0.0  # ten windows before it: unknown, not a rate
    r2 = binaryrisk.Recent([(float(i), 1) for i in range(100)])
    assert abs(r2.yes(100.0, 1.0) - 0.5) < 1e-9 and r2.yes(50.0, 1.0) == 0.5  # all YES: +0.5 once twenty have closed
    assert r2.yes(20.0, 1.0) == 0.5 and r2.yes(19.0, 1.0) == 0.0  # exactly twenty before t=20, nineteen before t=19


def rows(n=6000, seed=2):
    """Training sides where a high 'cz' pays and a low one loses: the fit must find it."""
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        x = [0.0] * len(binaryrisk.NAMES)
        ask = rnd.uniform(0.25, 0.9)
        cz = rnd.uniform(-2, 2)
        x[0] = math.log(ask / (1 - ask)); x[1] = 10 * (ask - 0.5) ** 2; x[2] = cz
        net = 0.3 * cz + rnd.gauss(0, 0.1)
        out.append((x, [net] * 5, ask))
    return out


def test_unit_the_fit_finds_a_paying_reading_and_the_bar_is_a_top_share():
    m = binaryrisk.fit(rows())
    assert set(m["cuts"]) == {"0.05", "0.1"} and m["cuts"]["0.05"] > m["cuts"]["0.1"]
    lo = [0.0] * len(binaryrisk.NAMES); hi = [0.0] * len(binaryrisk.NAMES)
    lo[2], hi[2] = -2.0, 2.0
    assert binaryrisk.predict(m, hi)[0] > binaryrisk.predict(m, lo)[0]
    assert binaryrisk.predict(m, hi)[0] >= m["cuts"]["0.05"]


def test_unit_the_never_bet_range():
    assert not binaryrisk.usable(0.45) and not binaryrisk.usable(0.50) and not binaryrisk.usable(0.55)
    assert not binaryrisk.usable(0.19) and binaryrisk.usable(0.44) and binaryrisk.usable(0.56) and binaryrisk.usable(0.20)


@pytest.fixture
def clean():
    yield
    ticks.history.clear()
    ticks.strikes._d.clear() if hasattr(ticks.strikes, "_d") else None


def shot(side="yes", ask=0.62):
    return LongShot(ticker="KXBTC15M-26NOV200000-00", title="t", rules="r", bid=ask - 0.01, ask=ask, volume=1.0, close_time="2026-11-20T00:00:00Z", hours_to_close=300.0 / 3600.0, series="KXBTC15M", side=side)


def test_unit_admits_refuses_without_a_model_and_in_the_dead_range(tmp_path, monkeypatch, clean):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    binaryrisk._cache.update(mtime=None, data=None)
    ok, why = binaryrisk.admits(s, shot(), 0.62)
    assert not ok and "no model" in why
    (tmp_path / "binary_risk_model.json").write_text(json.dumps(binaryrisk.fit(rows())))
    binaryrisk._cache.update(mtime=None, data=None)
    ok, why = binaryrisk.admits(s, shot(ask=0.50), 0.50)
    assert not ok and "not a bet" in why


def test_integration_the_shadow_step_never_says_no_and_notes_the_verdict(tmp_path, monkeypatch, clean):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    binaryrisk._cache.update(mtime=None, data=None)
    ctx = buychain.Ctx(s=s, spec=None)
    got = buychain.STEPS["binary_risk_shadow"](ctx, shot())
    assert got.ok and "would not buy" in got.why  # no model yet: a shadow, not a block
    monkeypatch.setattr(binaryrisk, "admits", lambda *a, **k: (True, "binary risk +0.20/$1 by hold"))
    got = buychain.STEPS["binary_risk_shadow"](ctx, shot())
    assert got.ok and got.why.startswith("shadow: would buy")
    monkeypatch.setattr(binaryrisk, "admits", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert buychain.STEPS["binary_risk_shadow"](ctx, shot()).ok  # a failing reading never stops a buy


def test_unit_the_step_is_a_named_lane_step_and_not_a_new_lane_version():
    from kalshi_moonshot import lanes

    assert "binary_risk_shadow" in lanes.BUY_STEPS


def test_integration_the_deciding_step_says_no_without_a_model_in_the_dead_range_and_on_error(tmp_path, monkeypatch, clean):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    binaryrisk._cache.update(mtime=None, data=None)
    ctx = buychain.Ctx(s=s, spec=None)
    got = buychain.STEPS["binary_risk"](ctx, shot())
    assert not got.ok and "no model" in got.why  # unlike the shadow step, a missing model is a no
    monkeypatch.setattr(binaryrisk, "admits", lambda *a, **k: (True, "binary risk +0.30/$1 by hold"))
    assert buychain.STEPS["binary_risk"](ctx, shot()).ok
    monkeypatch.setattr(binaryrisk, "admits", lambda *a, **k: (False, "binary risk: ask 0.50 is not a bet"))
    assert not buychain.STEPS["binary_risk"](ctx, shot(ask=0.50)).ok
    monkeypatch.setattr(binaryrisk, "admits", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    got = buychain.STEPS["binary_risk"](ctx, shot())
    assert not got.ok and "unreadable" in got.why  # a deciding step that fails does not buy


def test_unit_the_refit_is_wanted_by_either_step_and_only_when_the_lane_is_enabled():
    from types import SimpleNamespace as NS

    lane = lambda chain, on=True: NS(enabled=on, buy_chain=chain)
    assert binaryrisk.wanted({"a": lane(("price", "binary_risk"))})  # the deciding step: the bug of 11-20, the refit never started
    assert binaryrisk.wanted({"a": lane(("price", "binary_risk_shadow"))})
    assert not binaryrisk.wanted({"a": lane(("price", "risk_score")), "b": lane(("binary_risk",), on=False)})


def test_unit_a_prediction_never_exceeds_what_the_bet_can_pay():
    assert abs(binaryrisk.ceiling(0.98) - (0.02 - binaryrisk.fee(0.98)) / 0.98) < 1e-12 and binaryrisk.ceiling(0.98) < 0.03
    assert binaryrisk.ceiling(0.50) > 0.9 and binaryrisk.ceiling(0.20) > 3.0
    n = len(binaryrisk.NAMES)
    model = {"mu": [0.0] * n, "sd": [1.0] * n, "W": [[5.0] + [0.0] * n] * 5}  # a line that predicts +5 per $1 everywhere
    x = [0.0] * n
    assert binaryrisk.predict(model, x)[0] == 5.0
    assert binaryrisk.predict_capped(model, x, 0.98)[0] == binaryrisk.ceiling(0.98) < 0.03  # the 0.98 NO of 11-20 07:40: cut to about +0.02
    assert binaryrisk.predict_capped(model, x, 0.20)[0] == binaryrisk.ceiling(0.20)  # a cheap side may still pay a lot


def test_unit_the_bar_is_made_from_capped_predictions():
    r = rows()
    m = binaryrisk.fit(r)
    capped = sorted(binaryrisk.predict_capped(m, x, a)[0] for x, _, a in r[::2] if binaryrisk.usable(a))
    assert abs(m["cuts"]["0.05"] - capped[min(len(capped) - 1, int(len(capped) * 0.95))]) < 1e-9


def test_unit_a_model_built_before_the_cap_is_refit_at_once(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    started = []
    monkeypatch.setattr(binaryrisk, "refresh_recent", lambda *a, **k: False)
    monkeypatch.setattr(binaryrisk, "_fitting", None)
    monkeypatch.setattr(binaryrisk.subprocess, "Popen", lambda *a, **k: started.append(a) or type("P", (), {"poll": lambda self: None})())
    fresh = binaryrisk.fit(rows())
    assert fresh["capped"] is True
    (tmp_path / "binary_risk_model.json").write_text(json.dumps(fresh))
    binaryrisk._cache.update(mtime=None, data=None)
    assert binaryrisk.maybe_refit(s, now=fresh["built_at"] + 60.0) is False and not started  # a capped model fitted a minute ago: left alone
    old = {k: v for k, v in fresh.items() if k != "capped"}
    (tmp_path / "binary_risk_model.json").write_text(json.dumps(old))
    binaryrisk._cache.update(mtime=None, data=None)
    assert binaryrisk.maybe_refit(s, now=fresh["built_at"] + 60.0) is True and len(started) == 1  # no flag: refit now
