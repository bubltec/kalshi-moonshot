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
    i = binaryrisk.NAMES.index("czb")
    assert yes[i] > 0 > no[i]  # the blended cushion: a lead for YES, a deficit for NO
    assert abs(up["czb"] - (up["cz1"] + up["cz3"] + up["cz5"]) / 3.0) < 1e-12  # the same curve is a lead for YES and a deficit for NO
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






@pytest.fixture
def clean():
    yield
    ticks.history.clear()
    ticks.strikes._d.clear() if hasattr(ticks.strikes, "_d") else None


def shot(side="yes", ask=0.62):
    return LongShot(ticker="KXBTC15M-26NOV200000-00", title="t", rules="r", bid=ask - 0.01, ask=ask, volume=1.0, close_time="2026-11-20T00:00:00Z", hours_to_close=300.0 / 3600.0, series="KXBTC15M", side=side)


















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




def swing_model(tmp_path):
    (tmp_path / "binary_risk_model.json").write_text(json.dumps(binaryrisk.fit(rows())))
    binaryrisk._cache.update(mtime=None, data=None)


def commodity_shot(side="yes", ask=0.30):
    return LongShot(ticker="KXGOLD15M-26NOV200000-00", title="t", rules="r", bid=ask - 0.01, ask=ask, volume=1.0, close_time="2026-11-20T00:00:00Z", hours_to_close=800.0 / 3600.0, series="KXGOLD15M", side=side)










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
    assert late["czlb"] / late["czb"] < early["czlb"] / early["czb"]  # the ratio is the share of the market left
    yes = binaryrisk.side_vector(early, "yes", 0.60, 0.0, 0.0)
    no = binaryrisk.side_vector(early, "no", 0.40, 0.0, 0.0)
    i = binaryrisk.NAMES.index("czlb")
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


def test_unit_the_wave_shape_tells_a_line_from_a_hump_and_a_flattening():
    n = 3700
    T = [1_000_000.0 + i for i in range(n)]
    now = T[-1]

    def series(fn):  # the shape lives in the last 15 minutes; before that the path is flat at its first value
        return [100.0 * math.exp(0.002 * fn(max(0.0, (i - (n - 900)) / 899.0))) for i in range(n)]

    line = series(lambda s: s)
    hump = series(lambda s: 1.0 - 4.0 * (s - 0.5) ** 2)  # up then down
    flat_out = series(lambda s: math.log1p(9.0 * s) / math.log(10.0))  # fast then flattening (log)
    u = 0.0005
    sl, cv, ft = binaryrisk.shape(T, line, now, 15, u)
    assert sl > 0 and abs(cv) < 0.05 * abs(sl) and ft > 0.99  # a line: slope only, fits a line
    sl, cv, ft = binaryrisk.shape(T, hump, now, 15, u)
    assert cv < 0 and ft < 0.5  # a hump: bends over, a line fits badly
    sl, cv, ft = binaryrisk.shape(T, flat_out, now, 15, u)
    assert sl > 0 and cv < 0 and 0.5 < ft < 1.0  # a log shape: rising and flattening
    assert binaryrisk.shape(T, [100.0] * n, now, 15, u) == (0.0, 0.0, 1.0)  # flat


def test_unit_the_shape_readings_are_in_the_vector_and_curvature_flips_with_the_side():
    T, V = walk(4000)
    f = binaryrisk.readings(T, V, T[-1], V[-1], 600.0)
    assert f is not None and all(k in f for k in ("sl15", "sl30", "sl60", "cv15", "cv30", "cv60", "ft15", "ft30", "ft60"))
    yes = binaryrisk.side_vector(f, "yes", 0.60)
    no = binaryrisk.side_vector(f, "no", 0.40)
    for nm in ("sl30", "cv30"):
        i = binaryrisk.NAMES.index(nm)
        assert yes[i] == -no[i]
    i = binaryrisk.NAMES.index("ft30")
    assert yes[i] == no[i]  # how line-like the path is does not depend on the side
    assert len(yes) == len(binaryrisk.NAMES)


def _f(cz1, cz3, cz5):
    return {"cz1": cz1, "cz3": cz3, "cz5": cz5}


def test_unit_the_gate_needs_a_positive_cushion_on_every_average_and_three_minutes():
    assert binaryrisk.gate(_f(0.3, 0.2, 0.1), "yes", 600.0) is None
    assert binaryrisk.gate(_f(-0.3, -0.2, -0.1), "no", 600.0) is None  # a NO side is ahead when the cushions are negative
    # the copper 6:29 buy: the 1 minute average a hair ahead, the 3 and 5 minute ones well behind
    assert "weakest cushion" in binaryrisk.gate(_f(0.17, -0.60, -0.81), "yes", 120.0 + 60.0)
    assert "weakest cushion" in binaryrisk.gate(_f(0.17, 0.0, 0.5), "yes", 600.0)  # zero is not ahead
    assert "last 3" in binaryrisk.gate(_f(0.5, 0.5, 0.5), "yes", 179.0)  # inside the last three minutes
    assert binaryrisk.gate(_f(0.5, 0.5, 0.5), "yes", 180.0) is None




# ---- the price-free win-chance score and its shadow step (2026-11-21)
FLAT = ("left", "czb", "czlb", "tr13", "tr35", "vr", "amp15", "pos15", "pos30", "pos60", "mv15", "mv30", "mv60", "sl15", "sl30", "sl60", "cv15", "cv30", "cv60", "ft15", "ft30", "ft60", "dr24", "dr72")


def win_rows(n=6000, seed=3):
    """Training sides where a high cushion wins and a low one loses; the ask is unrelated to the outcome here."""
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        x = [0.0] * len(binaryrisk.NAMES)
        ask = rnd.uniform(0.25, 0.9)
        cz = rnd.uniform(-2, 2)
        x[0] = math.log(ask / (1 - ask)); x[1] = 10 * (ask - 0.5) ** 2; x[2] = cz
        won = 1.0 if rnd.random() < 1.0 / (1.0 + math.exp(-2.0 * cz)) else 0.0
        out.append((x, [(won - ask - binaryrisk.fee(ask)) / ask] * 5, ask))
    return out


def test_unit_the_win_chance_fit_finds_the_cushion_has_no_ask_reading_and_is_calibrated_at_the_top():
    m = binaryrisk.fit_win(win_rows())
    assert m["names"] == binaryrisk.NAMES[2:] and "logit_ask" not in m["names"] and "ask_off" not in m["names"]
    lo, hi = [0.0] * len(m["names"]), [0.0] * len(m["names"])
    i = m["names"].index("czb")
    lo[i], hi[i] = -1.5, 1.5
    assert binaryrisk.win_chance(m, hi) > 0.85 > 0.15 > binaryrisk.win_chance(m, lo)
    top = [c for c in m["calibration"] if c["n"] >= 100 and c["lo"] >= 0.9]
    assert top and all(abs(c["win"] - (c["lo"] + min(c["hi"], 1.0)) / 2.0) < 0.06 for c in top)  # the top bands win about as often as they say


def test_unit_the_win_chance_does_not_move_with_the_price():
    m = binaryrisk.fit_win(win_rows())
    f = {k: 0.0 for k in FLAT}
    f.update(czb=1.0, left=5.0)
    assert binaryrisk.win_vector(f, "yes") == binaryrisk.side_vector(f, "yes", 0.31)[2:] == binaryrisk.side_vector(f, "yes", 0.93)[2:]
    assert binaryrisk.win_chance(m, binaryrisk.win_vector(f, "yes")) > binaryrisk.win_chance(m, binaryrisk.win_vector(f, "no"))  # NO is the side's own view


def test_integration_the_win_chance_step_never_says_no_reads_every_ask_and_notes_the_readings(tmp_path, monkeypatch, clean):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    ctx = buychain.Ctx(s=s, spec=None)
    got = buychain.STEPS["win_chance_shadow"](ctx, shot())
    assert got.ok and "no model" in got.why  # no model yet: a shadow, not a block
    (tmp_path / "binary_win_model.json").write_text(json.dumps(binaryrisk.fit_win(win_rows())))
    f = {k: 0.0 for k in FLAT}
    f.update(cz1=1.0, cz3=1.0, cz5=1.0, czb=1.0, left=5.0)
    monkeypatch.setattr(binaryrisk, "_curve", lambda shot, now: (f, ""))
    notes = set()
    for ask in (0.30, 0.50, 0.93):  # no ask band: the dead range and the cheap side are read like any other
        got = buychain.STEPS["win_chance_shadow"](ctx, shot(ask=ask))
        assert got.ok and "win chance" in got.why and "cushion +1.0" in got.why
        notes.add(got.why)
    assert len(notes) == 1  # the same curve scores the same at any price
    monkeypatch.setattr(binaryrisk, "_curve", lambda shot, now: (None, "binary risk: only 2.0 minutes left (no buy inside the last 3)."))
    got = buychain.STEPS["win_chance_shadow"](ctx, shot())
    assert got.ok and "no win chance" in got.why and "2.0 minutes" in got.why  # the gate's reason, still no block
    monkeypatch.setattr(binaryrisk, "win_read", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert buychain.STEPS["win_chance_shadow"](ctx, shot()).ok


def test_unit_the_win_chance_step_is_a_named_shadow_step_and_starts_the_refit():
    from types import SimpleNamespace as NS

    from kalshi_moonshot import lanes

    assert "win_chance_shadow" in lanes.BUY_STEPS
    assert binaryrisk.wanted({"a": NS(enabled=True, buy_chain=("price", "win_chance_shadow"))})


# ---- the buy-side value check after the win chance (2026-11-21)
def _value_reset():
    binaryrisk._value["hist"].clear()
    binaryrisk._value["last"].clear()








