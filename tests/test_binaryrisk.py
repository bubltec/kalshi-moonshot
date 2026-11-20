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
    V = V[:-400] + [V[-1]] * 400  # flat for the last 400 s: every average equals the spot
    f = binaryrisk.readings(T, V, now, V[-1], 300.0)
    assert f is not None and all(abs(f[k]) < 1e-9 for k in ("cz1", "cz3", "cz5")) and f["left"] == 5.0  # spot at the strike: no cushion at any scale
    assert all(0.0 <= f[k] <= 1.0 for k in ("pos15", "pos30", "pos60"))


def test_unit_the_cushion_is_in_typical_moves_and_signed_by_side():
    T, V = walk(4000)
    now = T[-1]
    up = binaryrisk.readings(T, V, now, V[-1] * 0.99, 300.0)  # the strike well under the spot: YES is ahead
    assert up["cz1"] > 0 and up["cz3"] > 0 and up["cz5"] > 0
    yes = binaryrisk.side_vector(up, "yes", 0.60, 0.0, 0.0)
    no = binaryrisk.side_vector(up, "no", 0.40, 0.0, 0.0)
    for nm in ("cz1", "cz3", "cz5"):
        i = binaryrisk.NAMES.index(nm)
        assert yes[i] > 0 > no[i]  # the same curve is a lead for YES and a deficit for NO
    for nm in ("pos15", "pos30", "pos60"):
        i = binaryrisk.NAMES.index(nm)
        assert abs(yes[i] + no[i] - 1.0) < 1e-9  # the place in the range is the side's own view


def test_unit_the_drift_is_the_spots_move_over_a_day_and_three_days_signed_by_side():
    T, V = walk(4000)
    now, spot = T[-1], V[-1]
    none = binaryrisk.readings(T, V, now, spot, 600.0)
    assert none["dr24"] == 0.0 and none["dr72"] == 0.0  # no references: unknown, not a signal
    up = binaryrisk.readings(T, V, now, spot, 600.0, (spot * 0.97, spot * 0.90))  # the spot is up 3% on a day ago and 10% on three days ago
    assert up["dr24"] > 0 and up["dr72"] > 0
    down = binaryrisk.readings(T, V, now, spot, 600.0, (spot * 1.03, spot * 1.10))
    assert down["dr24"] < 0 and down["dr72"] < 0
    yes = binaryrisk.side_vector(up, "yes", 0.60)
    no = binaryrisk.side_vector(up, "no", 0.40)
    for nm in ("dr24", "dr72"):
        i = binaryrisk.NAMES.index(nm)
        assert yes[i] > 0 > no[i]  # a falling market helps NO, a rising one helps YES


def test_unit_the_references_are_a_background_template_read_from_memory(tmp_path, monkeypatch):
    binaryrisk._refs.clear()
    assert binaryrisk.refs_for("KP_GOLD") is None
    binaryrisk._refs["KP_GOLD"] = (time.time(), 4000.0, 3900.0)
    assert binaryrisk.refs_for("KP_GOLD") == (4000.0, 3900.0)
    binaryrisk._refs.clear()


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
    monkeypatch.setattr(binaryrisk, "refresh_refs", lambda *a, **k: False)
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


def test_unit_a_commodity_reads_kalshi_perp_reference_not_an_outside_feed():
    assert binaryrisk.curve_index("KXBTC15M") == "BRTI" or binaryrisk.curve_index("KXBTC15M")
    assert binaryrisk.curve_index("KXGOLD15M") == "KP_GOLD"
    assert binaryrisk.curve_index("KXNATGAS15M") == "KP_NATGAS"
    assert not str(binaryrisk.curve_index("KXGOLD15M")).startswith("BQ_")
    assert binaryrisk.curve_index("KXNOPE15M") is None


def test_unit_a_commodity_strike_is_the_perp_value_at_the_open_and_none_without_a_tick():
    close = 1_000_900.0
    T, V = [1_000_001.0, 1_000_010.0, 1_000_500.0], [2000.0, 2001.0, 2005.0]
    assert binaryrisk.open_strike(T, V, close) == 2000.0 or binaryrisk.open_strike(T, V, close) == 2001.0
    assert binaryrisk.open_strike([1_000_500.0], [2005.0], close) is None  # nothing within 25 s of the open
    assert binaryrisk.open_strike([], [], close) is None


def test_integration_a_commodity_is_scored_on_the_perp_curve_with_its_open_as_the_strike(tmp_path, monkeypatch, clean):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    (tmp_path / "binary_risk_model.json").write_text(json.dumps(binaryrisk.fit(rows())))
    binaryrisk._cache.update(mtime=None, data=None)
    now = time.time()
    close = now + 300.0
    T = [close - 900.0 - 3650.0 + i for i in range(4400)]
    V = [2000.0 * math.exp(0.00005 * ((i * 7) % 11 - 5)) for i in range(4400)]
    for t, v in zip(T, V):
        ticks.history.note("KP_GOLD", t, v)
    sh = LongShot(ticker="KXGOLD15M-26NOV200000-00", title="t", rules="r", bid=0.29, ask=0.30, volume=1.0, close_time="2026-11-20T00:00:00Z", hours_to_close=300.0 / 3600.0, series="KXGOLD15M", side="yes")
    ok, why = binaryrisk.admits(s, sh, 0.30, now=now)
    assert "no settlement index" not in why and "no Kalshi index" not in why  # it reads the perp curve now (the crypto-only refusal is gone)


def swing_model(tmp_path):
    (tmp_path / "binary_risk_model.json").write_text(json.dumps(binaryrisk.fit(rows())))
    binaryrisk._cache.update(mtime=None, data=None)


def commodity_shot(side="yes", ask=0.30):
    return LongShot(ticker="KXGOLD15M-26NOV200000-00", title="t", rules="r", bid=ask - 0.01, ask=ask, volume=1.0, close_time="2026-11-20T00:00:00Z", hours_to_close=800.0 / 3600.0, series="KXGOLD15M", side=side)


def test_unit_the_swing_step_is_a_named_step_a_lane_may_use():
    from kalshi_moonshot import lanes

    assert "swing_risk" in buychain.STEPS and "swing_risk" in lanes.BUY_STEPS
    assert "swing_risk" in binaryrisk.STEPS  # a lane naming it gets the refit


def test_unit_the_swing_bar_comes_from_the_markets_own_recent_predictions(tmp_path, monkeypatch, clean):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    swing_model(tmp_path)
    binaryrisk._swing["hist"].clear(); binaryrisk._swing["last"].clear()
    now = time.time()
    state = {"k": 2}
    seq = iter([i / 1000.0 for i in range(1000)])
    monkeypatch.setattr(binaryrisk, "_read", lambda *a, **k: ({"model": {}, "f": {}, "pred": next(seq), "k": state["k"]}, ""))
    # the first predictions only build the history: no bar yet, no buy
    ok, why = binaryrisk.swing_admits(s, commodity_shot(), 0.30, now=now)
    assert not ok and "bar is not known" in why
    for i in range(1, 400):  # distinct sides' times so each is stored
        binaryrisk._swing["last"].clear()
        binaryrisk.swing_admits(s, commodity_shot(), 0.30, now=now + i)
    binaryrisk._swing["last"].clear()
    hi = binaryrisk.swing_admits(s, commodity_shot(), 0.30, now=now + 500)  # 0.4 .. next predictions rising: the top of its own history
    assert "own predictions" in hi[1]
    monkeypatch.setattr(binaryrisk, "_read", lambda *a, **k: ({"model": {}, "f": {}, "pred": 5.0, "k": 2}, ""))
    binaryrisk._swing["last"].clear()
    assert binaryrisk.swing_admits(s, commodity_shot(), 0.30, now=now + 600)[0]  # far above its own history
    monkeypatch.setattr(binaryrisk, "_read", lambda *a, **k: ({"model": {}, "f": {}, "pred": 5.0, "k": 0}, ""))
    binaryrisk._swing["last"].clear()
    ok, why = binaryrisk.swing_admits(s, commodity_shot(), 0.30, now=now + 700)
    assert not ok and "hold, not a swing" in why  # the best exit is to hold: not a swing
    monkeypatch.setattr(binaryrisk, "_read", lambda *a, **k: ({"model": {}, "f": {}, "pred": -5.0, "k": 2}, ""))
    binaryrisk._swing["last"].clear()
    assert not binaryrisk.swing_admits(s, commodity_shot(), 0.30, now=now + 800)[0]  # below its own history


def test_integration_the_swing_step_says_no_without_a_model_and_on_error(tmp_path, monkeypatch, clean):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    binaryrisk._cache.update(mtime=None, data=None)
    ctx = buychain.Ctx(s=s, spec=None)
    got = buychain.STEPS["swing_risk"](ctx, commodity_shot())
    assert not got.ok and "no model" in got.why
    monkeypatch.setattr(binaryrisk, "swing_admits", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert not buychain.STEPS["swing_risk"](ctx, commodity_shot()).ok  # a failing reading never buys


def test_unit_the_commodity_early_swing_lane_block_loads_and_names_only_known_steps():
    import pathlib
    import tomllib

    from kalshi_moonshot import lanes

    d = tomllib.loads(pathlib.Path("docs/commodity_early_swing.lane.toml").read_text())
    steps = d["module"]["swing_commodity"]["steps"]
    assert steps[-1] == "swing_risk" and all(x in lanes.BUY_STEPS for x in steps)  # the risk model decides last
    lane = d["lane"]["commodity_early_swing"]
    assert lane["price"] == [0.20, 0.45] and lane["exit_module"] == "round_trip" and lane["paper_fill_model"] is True
    assert all(s.startswith("KX") and s.endswith("15M") for s in lane["series"])


def test_unit_a_blip_on_the_last_tick_is_not_a_cushion():
    T, V = walk(4000)
    V = V[:-400] + [V[-401]] * 399 + [V[-401] * 1.002]  # flat, then one tick 0.2% up (the blip)
    now = T[-1]
    strike = V[-401]
    f = binaryrisk.readings(T, V, now, strike, 600.0)
    assert f is not None
    last_tick_cushion = math.log(V[-1] / strike)
    assert last_tick_cushion > 0
    # the averaged cushions are a fraction of what the last tick alone shows, the shorter the average the more of the blip
    assert 0 < f["cz5"] < f["cz3"] < f["cz1"]
    assert f["pos15"] < 1.0 and f["pos30"] < 1.0


def test_unit_the_trend_is_the_direction_of_the_averages_signed_by_side():
    T, V = walk(4000, drift=0.00003)  # a steady rise
    f = binaryrisk.readings(T, V, T[-1], V[-1] * 0.99, 600.0)
    assert f is not None and f["tr13"] > 0 and f["tr35"] > 0  # the shorter average is above the longer one
    yes = binaryrisk.side_vector(f, "yes", 0.60, 0.0, 0.0)
    no = binaryrisk.side_vector(f, "no", 0.40, 0.0, 0.0)
    for nm in ("tr13", "tr35"):
        i = binaryrisk.NAMES.index(nm)
        assert yes[i] > 0 > no[i]  # a rising spot helps YES and hurts NO


def test_unit_the_cushion_is_also_read_against_the_time_left():
    T, V = walk(4000)
    strike = V[-1] * 0.99
    early = binaryrisk.readings(T, V, T[-1], strike, 840.0)
    late = binaryrisk.readings(T, V, T[-1], strike, 90.0)
    assert early is not None and late is not None
    # the same spot and strike: the cushion in typical moves grows as time runs down, but its time-left term is smaller
    assert late["cz3"] > early["cz3"] > 0
    assert late["czl3"] / late["cz3"] < early["czl3"] / early["cz3"]  # the ratio is the share of the market left
    yes = binaryrisk.side_vector(early, "yes", 0.60, 0.0, 0.0)
    no = binaryrisk.side_vector(early, "no", 0.40, 0.0, 0.0)
    i = binaryrisk.NAMES.index("czl3")
    assert yes[i] > 0 > no[i]


def test_unit_the_volatility_ratio_and_the_range_are_read_in_typical_minutes():
    T, V = walk(4000)
    f = binaryrisk.readings(T, V, T[-1], V[-1], 600.0)
    assert f is not None and f["amp15"] > 0 and f["vr"] > -1.0
    i, j = binaryrisk.NAMES.index("vr"), binaryrisk.NAMES.index("amp15")
    yes = binaryrisk.side_vector(f, "yes", 0.60)
    no = binaryrisk.side_vector(f, "no", 0.40)
    assert yes[i] == no[i] == f["vr"] and yes[j] == no[j] == f["amp15"]  # side-neutral: the wave's size does not depend on the side
    sped = list(V[:3000]) + [V[3000] * math.exp(0.0006 * ((k % 2) * 2 - 1)) for k in range(1000)]  # calm hour, jumpy last 15 minutes
    g = binaryrisk.readings(T, sped, T[-1], sped[-1], 600.0)
    assert g is not None and g["vr"] > f["vr"]
