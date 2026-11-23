"""The wave-top lane (wavetop.py; 2026-11-21 the user's hand scalp, rebuilt 2026-11-22): the detector on the 60-second average against the target, the buy step's gates, and the two sale steps (wave_take, wave_salvage), alone and through the lane file.

The user's two reviews of the first version, which bought AFTER the flip and on the wrong end: "it was above the target on a spike ... it should have bought NO" (ZEC 18:18) and "it was below the strike (already no) ... It then flipped big time" (XRP 02:31). The detector tests here hold the three shapes: a peak above the target (NO, before the flip), a dip below it (YES) and a flip that crosses the target (nothing)."""
import dataclasses
import time

import pytest

from kalshi_moonshot import buychain, config, lanes, memguard, sellchain, wavetop
from kalshi_moonshot import ticks as ticks_mod
from kalshi_moonshot.strategy import LongShot

OPEN = 1_900_000_800.0  # a quarter-hour
INDEX = "BRTI"
START = 100_000.0

TOML = """
[lane.wt]
estimator = "ladder"
needs_model_edge = false
fixed_stake = 7.5
sides = ["yes", "no"]
price = [0.18, 0.40]
hours = [0.0, 0.26]
entry_elapsed = [0, 8]
series = ["KXBTC15M"]
timed_suffix = "15M"
wave_top_trend_hours = 1.0
buy_module = "m"
exit_module = "x"

[module.m]
steps = ["price", "wave_top"]

[exit.x]
steps = ["wave_take", "wave_salvage"]
curve_risk_live = true

[exit.x.profit]

[exit.x.loss]
"""


def background(lead=3700, tilt=0.0):
    """One tick a second from `lead` s before the open: a triangle of 10 every 200 s (slope 0.05 a second), the legs the wave must beat. tilt: a drift over the hours before the open."""
    T, V, v = [], [], START
    t0 = int(OPEN) - lead
    for k in range(lead):
        v += (0.05 if (k // 200) % 2 == 0 else -0.05) + tilt
        T.append(float(t0 + k))
        V.append(v)
    return T, V, v


def segments(T, V, v, plan):
    """Append (seconds, total change) segments to the path from the open."""
    t = int(OPEN)
    for seconds, total in plan:
        for _ in range(seconds):
            v += total / seconds
            T.append(float(t))
            V.append(v)
            t += 1
    return T, V, float(t - 1)


def peak_path(direction=1, lead=3700, tilt=0.0):
    """A sharp rise (fall, with direction -1) of 24 over 60 s, 40 s flat, then 30 back over 30 s: the spike and the start of its turn."""
    T, V, v = background(lead, tilt)
    return segments(T, V, v, [(60, 24.0 * direction), (40, 0.0), (30, -30.0 * direction), (60, -6.0 * direction)])


def reads(T, V, strike, first, last):
    """The detector at each second from `first` to `last` (seconds after the open): [(second, wave or None, why)]."""
    out = []
    for off in range(first, last):
        now = OPEN + off
        got = [(t, v) for t, v in zip(T, V, strict=True) if OPEN - 3700 <= t <= now]
        w, why = wavetop.read([g[0] for g in got], [g[1] for g in got], OPEN, now, strike)
        out.append((off, w, why))
    return out


@pytest.fixture
def mem():
    ticks_mod.history.clear()
    ticks_mod.mids.clear()
    ticks_mod.strikes._v.clear()
    wavetop._memo.clear()
    yield
    ticks_mod.history.clear()
    ticks_mod.mids.clear()
    ticks_mod.strikes._v.clear()
    wavetop._memo.clear()


def feed(T, V):
    ticks_mod.history.warm(INDEX, list(zip(T, V, strict=True)), T[0] - 1.0)


def spec(tmp_path, monkeypatch, trend=1.0):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(TOML.replace("wave_top_trend_hours = 1.0", f"wave_top_trend_hours = {trend}"))
    return lanes.lane_specs(config.load())["wt"]


TICKER = "KXBTC15M-30JAN010015-15"


def shot(now, side, ask=0.30):
    left = OPEN + 900.0 - now
    return LongShot(TICKER, "t", "", ask - 0.01, ask, left / 3600.0, "2099-01-01T00:00:00Z", left / 3600.0, lane="wt", series="KXBTC15M", side=side, event_ticker="KXBTC15M-30JAN010015")


def ask_step(sp, sh, now):
    return buychain.STEPS["wave_top"](buychain.Ctx(config.load(), sp, now=now), sh)


# ---- the detector ----
def test_unit_a_spike_above_the_target_is_a_peak_bought_as_NO_before_the_flip():
    T, V, _ = peak_path()
    strike = START - 5.0  # the whole leg stays above the target
    found = [(off, w) for off, w, _ in reads(T, V, strike, 60, 190) if w is not None]
    assert found, "a peak above the target whose spot has crossed back under its average is a signal"
    assert all(w.d == 1 and w.side == "no" for _, w in found)
    first = found[0][0]
    top = max(V)
    now_raw = next(v for t, v in zip(T, V, strict=True) if t == OPEN + first)
    assert now_raw > top - 12.0  # the signal comes with the spot still near its top, not after the 30-point fall is done
    assert first < 130  # before the fall has finished (it runs 100 to 130 s)


def test_unit_a_fall_below_the_target_is_a_dip_bought_as_YES():
    T, V, _ = peak_path(direction=-1)
    strike = START + 15.0  # the whole fall stays under the target
    found = [(off, w) for off, w, _ in reads(T, V, strike, 60, 190) if w is not None]
    assert found and all(w.d == -1 and w.side == "yes" for _, w in found)


def test_unit_a_rise_that_began_under_the_target_is_a_flip_and_not_a_peak():
    T, V, _ = peak_path()
    strike = START + 3.0  # the leg begins under the target and crosses it
    why = {w is None and "other side of the target" in why for _, w, why in reads(T, V, strike, 60, 190)}
    assert True in why
    assert all(w is None for _, w, _ in reads(T, V, strike, 60, 190))


def test_unit_a_rise_with_the_average_under_the_target_is_no_peak():
    T, V, _ = peak_path()
    strike = START + 60.0
    got = reads(T, V, strike, 60, 190)
    assert all(w is None for _, w, _ in got) and any("under the target" in why for _, _, why in got)


def test_unit_a_gentle_rise_is_not_bigger_than_the_legs_before_it():
    T, V, v = background()
    T, V, _ = segments(T, V, v, [(150, 6.0), (60, 0.0), (60, -6.0)])  # slope 0.04 a second, under the triangle's 0.05
    assert all(w is None for _, w, _ in reads(T, V, START - 5.0, 60, 260))


def test_unit_the_buy_closes_once_the_average_has_turned():
    T, V, _ = peak_path()
    got = reads(T, V, START - 5.0, 60, 200)
    assert any(w is not None for off, w, _ in got if off < 140)
    assert all(w is None for off, w, _ in got if off >= 160)  # the flip is done: the fall is no dip (it stays above the target) and the rise is behind


def test_unit_the_history_runs_through_the_open_without_a_cut():
    T, V, _ = peak_path()
    late = [(t, v) for t, v in zip(T, V, strict=True) if t >= OPEN - 600]  # the history starts ten minutes before the open: the hour before it is not there
    w, why = wavetop.read([x[0] for x in late], [x[1] for x in late], OPEN, OPEN + 100, START)
    assert w is None and "does not cover" in why


def test_unit_the_average_is_the_trailing_sixty_second_mean_and_waits_for_a_full_span():
    got = wavetop.average([float(k) for k in range(130)])
    assert got[58] is None and got[59] == pytest.approx(29.5) and got[129] == pytest.approx(99.5)
    holey = [1.0] * 130
    holey[50] = None
    out = wavetop.average(holey)
    assert out[60] is None and out[109] is None and out[110] == pytest.approx(1.0)


# ---- the buy step ----
def test_unit_the_lane_file_names_the_steps_and_the_fields(tmp_path, monkeypatch):
    sp = spec(tmp_path, monkeypatch)
    assert sp.buy_chain == ("price", "wave_top") and sp.sell_chain == ("wave_take", "wave_salvage") and sp.wave_top_trend_hours == 1.0
    assert "wave_top" in lanes.BUY_STEPS and {"wave_take", "wave_salvage"} <= set(sellchain.SELL_STEPS) and sp.entry_elapsed == (0.0, 8.0)
    assert not hasattr(sp, "wave_top_quantile")  # the quantile gate was not asked for and is gone


def test_integration_the_step_buys_NO_on_a_peak_from_memory_only(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    T, V, _ = peak_path()
    feed(T, V)
    ticks_mod.strikes.note(TICKER, START - 5.0)
    signal = next(off for off, w, _ in reads(T, V, START - 5.0, 60, 190) if w is not None)
    now = OPEN + signal
    with memguard.decision("buy"):  # a SELECT or a Kalshi GET inside raises
        no, yes = ask_step(sp, shot(now, "no"), now), ask_step(sp, shot(now, "yes"), now)
    assert no.ok and "peak" in no.why and "buys NO" in no.why
    assert not yes.ok and "the bet is NO" in yes.why


def test_integration_a_market_with_no_target_in_memory_is_a_no(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    T, V, _ = peak_path()
    feed(T, V)
    now = OPEN + 100
    assert "target is not in memory" in ask_step(sp, shot(now, "no"), now).why


def test_integration_a_dip_bet_is_not_taken_after_a_rising_hour_and_is_taken_after_a_falling_one(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    T, V, _ = peak_path(direction=-1, lead=4600, tilt=0.004)  # the spot is about 14 higher at the open than an hour before
    assert V[4600] > V[1000]
    strike = max(V) + 5.0
    feed(T, V)
    ticks_mod.strikes.note(TICKER, strike)
    signal = next(off for off, w, _ in reads(T, V, strike, 60, 190) if w is not None)
    now = OPEN + signal
    rising = ask_step(sp, shot(now, "yes"), now)
    assert not rising.ok and "rising 1 h" in rising.why
    assert ask_step(spec(tmp_path, monkeypatch, trend=0), shot(now, "yes"), now).ok  # no trend check: the dip bet is taken
    ticks_mod.history.clear()
    wavetop._memo.clear()
    T, V, _ = peak_path(direction=-1, lead=4600, tilt=-0.004)
    strike = max(V) + 5.0
    ticks_mod.strikes.note(TICKER, strike)
    feed(T, V)
    falling = ask_step(spec(tmp_path, monkeypatch), shot(now, "yes"), now)
    assert falling.ok, falling.why
    ticks_mod.history.clear()
    wavetop._memo.clear()
    T, V, _ = peak_path(direction=-1)  # the history starts under an hour and ten minutes before the open: the value an hour before is not in memory
    ticks_mod.strikes.note(TICKER, max(V) + 5.0)
    feed(T, V)
    assert "not in memory" in ask_step(spec(tmp_path, monkeypatch), shot(now, "yes"), now).why


def test_integration_no_history_or_no_index_is_a_no(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    _T, _V, _ = peak_path()
    ticks_mod.strikes.note(TICKER, START - 5.0)
    now = OPEN + 100
    assert "no tick history" in ask_step(sp, shot(now, "no"), now).why
    sh = dataclasses.replace(shot(now, "no"), series="KXNOSUCH15M")
    assert not ask_step(sp, sh, now).ok


def test_unit_one_read_is_fast(mem):
    T, V, _ = peak_path()
    began = time.perf_counter()
    for _ in range(5):
        wavetop.read(T, V, OPEN, OPEN + 100, START - 5.0)
    assert (time.perf_counter() - began) / 5 < 0.03  # half the 60 ms budget of a decision


# ---- the salvage sale ----
def quotes(bids, start):
    """(time, YES bid, YES ask) one a second from `start`."""
    return [(start + k, b, b + 0.01) for k, b in enumerate(bids)]


def flat_spot(seconds, value=100.0, drift=0.0):
    return [OPEN + k for k in range(seconds)], [value + drift * k for k in range(seconds)]


def test_unit_a_bet_that_never_dipped_is_held():
    q = quotes([0.30] * 200 + [0.30 + 0.002 * k for k in range(200)], OPEN)
    sell, info = wavetop.salvage(q, "yes", OPEN, OPEN + 200, OPEN + 399, *flat_spot(400))
    assert not sell and not info["armed"]


def test_unit_a_bet_that_dipped_is_sold_once_its_climb_fades_and_the_spot_stops():
    before = [0.30 + 0.02 * ((k // 30) % 2) for k in range(200)]  # the bid's own typical minute before the buy is about 0.02
    dip = [0.30 - 0.004 * k for k in range(1, 31)]  # down to 0.18
    climb = [0.18 + 0.004 * k for k in range(1, 31)]  # back to 0.30
    stall = [0.30] * 20
    q = quotes(before + dip + climb + stall, OPEN)
    T, V = flat_spot(400)
    mid_climb = OPEN + 200 + 30 + 15
    assert not wavetop.salvage(q, "yes", OPEN, OPEN + 200, mid_climb, T, V)[0]  # still climbing at the run's pace
    sell, info = wavetop.salvage(q, "yes", OPEN, OPEN + 200, OPEN + 200 + 30 + 30 + 19, T, V)
    assert sell and info["armed"] and info["low"] == pytest.approx(0.18) and info["recent"] <= 0.25 * info["run"]


def test_unit_it_is_not_sold_while_the_spot_still_moves_the_bets_way():
    before = [0.30 + 0.02 * ((k // 30) % 2) for k in range(200)]
    q = quotes(before + [0.30 - 0.004 * k for k in range(1, 31)] + [0.18 + 0.004 * k for k in range(1, 31)] + [0.30] * 20, OPEN)
    now = OPEN + 200 + 30 + 30 + 19
    T, V = flat_spot(400, drift=0.5)  # a YES bet gains as the spot rises: it is still rising
    assert not wavetop.salvage(q, "yes", OPEN, OPEN + 200, now, T, V)[0]
    T, V = flat_spot(400, drift=-0.5)
    assert wavetop.salvage(q, "yes", OPEN, OPEN + 200, now, T, V)[0]
    no_q = [(t, 1.0 - a, 1.0 - b) for t, b, a in q]  # the same path seen from the NO side
    assert wavetop.salvage(no_q, "no", OPEN, OPEN + 200, now, *flat_spot(400, drift=0.5))[0]  # a NO bet gains as the spot falls: a rising spot has stopped helping it


def test_integration_the_sell_step_sells_a_dipped_bet(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import curverisk, watcher
    from tests.test_risk_exit_flow import CLOSE, held
    from tests.test_risk_exit_flow import TICKER as HELD

    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['wave_salvage']")
    t = w.targets[(HELD, "risk_only")]
    t.wave_whipsaw = 0.0
    opened = CLOSE - 900.0
    t.bought_ts = opened + 200.0
    before = [0.80 + 0.02 * ((k // 30) % 2) for k in range(200)]
    bids = before + [0.80 - 0.004 * k for k in range(1, 31)] + [0.68 + 0.004 * k for k in range(1, 31)] + [0.80] * 20
    for k, b in enumerate(bids):
        ticks_mod.mids.note(HELD, opened + k, b, b + 0.01)
    for k in range(-400, len(bids)):
        ticks_mod.history.note("BRTI", opened + k, 100.0)
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    now = opened + len(bids) - 1
    c = watcher._SellWalk(30.0, 0.83, 0.78, None)
    with memguard.decision("sell"):
        why = w._sell_wave_salvage(t, risk, 0.80, now, CLOSE - now, -0.02, c)
    assert why and why.startswith("wave salvage") and "typical minute" in why
    early = w._sell_wave_salvage(t, risk, 0.74, opened + 245, CLOSE - opened - 245, -0.1, c)
    assert early is None  # mid-climb: held
    t.bought_ts = 0.0
    assert w._sell_wave_salvage(t, risk, 0.80, now, CLOSE - now, -0.02, c) is None  # no buy time: nothing is read


def test_integration_the_salvage_does_not_sell_a_bet_that_is_winning_on_the_spot(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import curverisk, watcher
    from tests.test_risk_exit_flow import CLOSE, held
    from tests.test_risk_exit_flow import TICKER as HELD

    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['wave_salvage']")
    t = w.targets[(HELD, "risk_only")]
    t.wave_whipsaw = 0.0
    t.side = "yes"
    opened = CLOSE - 900.0
    t.bought_ts = opened + 200.0
    before = [0.80 + 0.02 * ((k // 30) % 2) for k in range(200)]
    bids = before + [0.80 - 0.004 * k for k in range(1, 31)] + [0.68 + 0.004 * k for k in range(1, 31)] + [0.80] * 20
    for k, b in enumerate(bids):
        ticks_mod.mids.note(HELD, opened + k, b, b + 0.01)
    for k in range(-1200, len(bids)):
        ticks_mod.history.note("BRTI", opened + k, 100.0 + 0.01 * (-1) ** k)
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    now = opened + len(bids) - 1
    c = watcher._SellWalk(30.0, 0.83, 0.78, None)
    t.shock_index = "BRTI"
    t.shock_ref = 101.0  # the spot's average is under the target: a YES bet that is losing on the spot
    with memguard.decision("sell"):
        assert w._sell_wave_salvage(t, risk, 0.80, now, CLOSE - now, -0.02, c)
    t.shock_ref = 99.0  # over the target: the YES bet is winning now
    c.facts = None  # a new check
    with memguard.decision("sell"):
        assert w._sell_wave_salvage(t, risk, 0.80, now, CLOSE - now, -0.02, c) is None


# ---- the take sales (wave_take) ----
def spot_feed(now, level, wobble=0.01):
    T = [now - 1500.0 + k for k in range(1501)]
    V = [level + wobble * (-1) ** k for k in range(1501)]
    ticks_mod.history.clear()
    ticks_mod.history.warm(INDEX, list(zip(T, V, strict=True)), T[0] - 1.0)


def take_target(tmp_path, monkeypatch, entry, unit=0.10):
    from tests.test_risk_exit_flow import CLOSE, held
    from tests.test_risk_exit_flow import TICKER as HELD

    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['wave_take']")
    t = w.targets[(HELD, "risk_only")]
    t.entry, t.bought_ts, t.side = entry, CLOSE - 800.0, "yes"
    t.shock_index, t.shock_ref = INDEX, 100.0
    t.wave_whipsaw = 0.0  # the is_whipsaw_behavior switch is off here (it has its own tests)
    t.wave_arm_swings = 0.0  # the loss backup arms on any gain here; its own tests set one swing
    monkeypatch.setattr(w, "_risk_unit", lambda tt, now: unit)  # the bid's typical minute
    return w, t, CLOSE


def asker(w, t, close):
    from kalshi_moonshot import curverisk, watcher

    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    c = watcher._SellWalk(30.0, 0.83, 0.78, None)

    def ask(bid, left):
        c.facts = None  # each ask is a new check: a new board
        with memguard.decision("sell"):
            return w._sell_wave_take(t, risk, bid, close - left, left, 0.0, c)

    return ask


def test_unit_the_spot_reading_and_the_chance_of_losing():
    now = 1_000_000.0
    T = [now - 700.0 + k for k in range(701)]
    V = [101.0 + 0.01 * (-1) ** k for k in range(701)]
    cushion, unit = wavetop.spot_read(T, V, now, 100.0, "yes")
    assert cushion == pytest.approx(1.0, abs=0.01) and unit == pytest.approx(0.02 * 7.746, abs=0.005)
    assert wavetop.spot_read(T, V, now, 100.0, "no")[0] == pytest.approx(-1.0, abs=0.01)  # a NO bet reads it the other way
    assert wavetop.spot_read(T, V, now + 60.0, 100.0, "yes") is None  # stale ticks
    assert wavetop.spot_read(T[-30:], V[-30:], now, 100.0, "yes") is None  # too little history
    assert wavetop.p_lose(1.0, 1.0, 60.0) == pytest.approx(0.1587, abs=0.001)  # one typical move of cushion with a minute left
    assert wavetop.p_lose(1.0, 1.0, 240.0) == pytest.approx(0.3085, abs=0.001) and wavetop.p_lose(1.0, 1.0, 15.0) == pytest.approx(0.0228, abs=0.001)  # more time, more chance to lose
    assert wavetop.p_lose(-1.0, 1.0, 60.0) > 0.8


def test_integration_the_staircase_sells_the_first_lower_low_ahead_and_no_fixed_40c_line(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.30, unit=0.20)  # a reversal of 0.25 x 0.20 x sqrt(60 / 60) = 0.05 at a minute left
    ask = asker(w, t, close)
    for bid in (0.28, 0.36, 0.41, 0.35, 0.41, 0.48, 0.41):  # a climb, a pullback of 0.06, a bounce of 0.06 (the low 0.35 is confirmed), a higher high, a pullback
        assert ask(bid, 60) is None
    assert ask(0.36, 60) is None  # a higher low than... it is still above the last swing low 0.35
    why = ask(0.34, 60)
    assert why and why.startswith("wave take") and "last swing low 0.35" in why and "a gain of" in why and "swings of 0.050" in why
    t.wave_dir, t.wave_hi, t.wave_lo, t.wave_last_low = 0, 0.0, 0.0, 0.0
    assert ask(0.70, 50) is None  # +0.40 and over is no sale by itself (user, 2026-11-22: the 40c line is out): only the staircase and the hold-value sale sell


def test_integration_the_backup_accepts_a_small_loss_on_a_bet_that_was_ahead(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.30, unit=0.20)
    ask = asker(w, t, close)
    for bid in (0.36, 0.42, 0.36, 0.33, 0.39, 0.45, 0.39, 0.34):  # ahead at 0.42, a swing low of 0.33 confirmed, a higher high, then back toward it
        assert ask(bid, 60) is None
    why = ask(0.32, 60)  # under the last swing low and under the cost: the bet had been ahead
    assert why and "a small loss of" in why and "had been ahead" in why


def test_integration_a_bet_that_was_never_ahead_is_not_sold_by_the_staircase(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.30, unit=0.20)
    ask = asker(w, t, close)
    for bid in (0.29, 0.31, 0.26, 0.28, 0.33, 0.20, 0.15):  # never ahead after both fees: the salvage's business
        assert ask(bid, 60) is None


def test_integration_the_reversal_is_wide_with_time_left_and_narrows_toward_the_close(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.30, unit=0.20)
    ask = asker(w, t, close)
    for bid in (0.36, 0.42, 0.36, 0.42, 0.50):
        ask(bid, 600)  # a lot of time left: swings of 0.25 x 0.20 x sqrt(10) = 0.158: a 0.06 pullback is no swing
    assert t.wave_dir >= 0 and t.wave_last_low == 0.0
    t.wave_dir, t.wave_hi, t.wave_lo, t.wave_last_low = 0, 0.0, 0.0, 0.0
    for bid in (0.36, 0.42, 0.36, 0.42):
        ask(bid, 60)  # a minute left: swings of 0.05
    assert t.wave_last_low == pytest.approx(0.36)


def test_integration_in_the_high_zone_it_holds_only_while_it_cannot_likely_lose(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.50, unit=0.10)
    ask = asker(w, t, close)
    spot_feed(close - 2.0, 102.0)  # a cushion of 2 against a typical move of 0.15: out of reach with 30 s left
    assert ask(0.85, 30) is None and ask(0.85, 29) is None  # held: it will not lose
    spot_feed(close - 2.0, 100.03)  # a cushion of 0.03: the bet could still be lost
    why = ask(0.85, 28)
    assert why and "high zone" in why and "chance of losing" in why
    t.wave_peak_bid = 0.0
    t.wave_dir, t.wave_hi, t.wave_lo, t.wave_last_low = 0, 0.0, 0.0, 0.0
    assert ask(0.75, 28) is None  # under 0.80 the zone's comparison is not asked


def test_integration_the_cushion_widens_the_swing_with_the_spot_on_the_bets_side_and_tightens_it_on_the_wrong_side(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.30, unit=0.20)
    ask = asker(w, t, close)
    spot_feed(close - 2.0, 102.0)  # far over the target: P_lose near 0, the factor near 2
    t.wave_dir, t.wave_hi, t.wave_lo, t.wave_last_low = 0, 0.0, 0.0, 0.0
    for bid in (0.36, 0.42, 0.36, 0.42, 0.36, 0.30):  # swings of 0.05 x 2 = 0.10: the 0.06 pullbacks are no swing, so there is no low to fall under
        assert ask(bid, 60) is None
    assert t.wave_last_low == 0.0
    spot_feed(close - 2.0, 98.0)  # under the target: P_lose near 1, the factor near 0: swings of 1c
    t.wave_dir, t.wave_hi, t.wave_lo, t.wave_last_low, t.wave_best_net = 0, 0.0, 0.0, 0.0, -9.0
    for bid in (0.36, 0.42, 0.40, 0.42, 0.40):
        ask(bid, 60)
    assert t.wave_last_low == pytest.approx(0.40)  # the 2c pullbacks are swings now
    t.wave_cushion = False
    t.wave_dir, t.wave_hi, t.wave_lo, t.wave_last_low, t.wave_best_net = 0, 0.0, 0.0, 0.0, -9.0
    for bid in (0.36, 0.42, 0.40, 0.42, 0.40):
        ask(bid, 60)
    assert t.wave_last_low == 0.0  # with the cushion off the swing is 0.05 again


def test_integration_the_exponent_sets_how_fast_the_swing_narrows(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.30, unit=0.20)
    ticks_mod.history.clear()  # no spot reading: the factor is 1
    ask = asker(w, t, close)
    t.wave_rev_p = 1.0  # at 300 s left: 0.25 x 0.20 x 5 = 0.25 (0.5 would give 0.11)
    for bid in (0.36, 0.42, 0.36, 0.42, 0.36):
        ask(bid, 300)
    assert t.wave_last_low == 0.0
    t.wave_rev_p = 0.5
    t.wave_dir, t.wave_hi, t.wave_lo, t.wave_last_low = 0, 0.0, 0.0, 0.0
    for bid in (0.36, 0.42, 0.30, 0.42, 0.40):
        ask(bid, 300)
    assert t.wave_last_low == pytest.approx(0.30)


def switch_target(tmp_path, monkeypatch, entry=0.30):
    """A wave bet with the whipsaw variant ON (2.16 typical minutes; the bid's typical minute is 0.10 here, so a range of 0.216), bought 800 s before the close."""
    w, t, close = take_target(tmp_path, monkeypatch, entry, unit=0.10)
    t.wave_whipsaw = 2.16
    return w, t, close


def test_integration_the_whipsaw_variant_is_read_on_every_tick_and_never_holds_the_staircase(tmp_path, monkeypatch, mem):
    # a calm bet: the bid's range stays at 0.12 (1.2 typical minutes): not the whipsaw shape, the staircase works as it does
    w, t, close = switch_target(tmp_path, monkeypatch)
    ask = asker(w, t, close)
    for bid in (0.36, 0.42, 0.35, 0.41, 0.48, 0.41, 0.36):
        assert ask(bid, 60) is None
    assert t.wave_kind == "" and t.wave_range_u == pytest.approx(1.3, abs=0.1)
    why = ask(0.34, 60)
    assert why and "last swing low" in why


def test_integration_no_blackout_a_sale_right_after_the_buy_goes_through(tmp_path, monkeypatch, mem):
    # 2026-11-23 SILVER 22:00: up 48% inside the first 120 s and nothing sold; no wait, no shape time
    w, t, close = switch_target(tmp_path, monkeypatch)
    ask = asker(w, t, close)
    t.wave_best_net = 0.05
    t.wave_dir, t.wave_last_low = -1, 0.35
    t.wave_hi = t.wave_lo = 0.30
    assert ask(0.20, 790)  # 10 s after the buy


def test_integration_a_bet_following_the_whipsaw_shape_widens_its_swing_to_its_own_range_and_is_still_read(tmp_path, monkeypatch, mem):
    w, t, close = switch_target(tmp_path, monkeypatch)
    ask = asker(w, t, close)
    for bid in (0.30, 0.55, 0.30, 0.58):  # the bet's own range so far reaches 0.28: 2.8 typical minutes, over the shape's 2.16
        assert ask(bid, 600) is None
    assert t.wave_kind == "whipsaw" and t.wave_range_u == pytest.approx(2.5, abs=0.1)
    assert t.wave_path_hi == pytest.approx(0.58) and t.wave_path_lo == pytest.approx(0.30)  # the price is read on every check
    assert ask(0.50, 600) is None  # a 0.08 fall is inside the whipsaw's own range: ordinary motion, no reversal
    assert ask(0.28, 600)  # a fall out of its range: the climb is done and the staircase sells


def test_integration_a_spike_starts_a_ride_that_is_held_while_the_bid_rises_and_sold_when_it_peaks(tmp_path, monkeypatch, mem):
    # the spike trail (N 20 s, z 5, trail 1.0): at a typical minute of 0.10 the bid's usual 20 s move is 0.058
    from tests.test_risk_exit_flow import TICKER as HELD

    w, t, close = take_target(tmp_path, monkeypatch, 0.39, unit=0.10)
    t.wave_spike, t.wave_spike_s, t.wave_spike_tr = 5.0, 20.0, 1.0
    now = close - 600.0
    for k in range(21):  # 0.39 -> 0.70 in 20 s: a rise of 0.31, 5.4 usual moves
        b = 0.39 + 0.31 * k / 20.0
        ticks_mod.mids.note(HELD, now - 20 + k, b, b + 0.01)
    ask = asker(w, t, close)
    assert ask(0.70, 600) is None and t.wave_ride  # started riding, nothing sold on the way up
    assert ask(0.74, 600) is None and t.wave_ride_high == pytest.approx(0.74)  # still rising: let it ride
    assert ask(0.70, 600) is None  # 0.04 under the high is inside the trail (0.058)
    why = ask(0.67, 600)
    assert why and "the spike peaked at 0.74" in why and "end of the ride" in why
    # a slow rise (0.31 over ten minutes) is not a spike
    ticks_mod.mids.clear()
    w2, t2, close2 = take_target(tmp_path, monkeypatch, 0.39, unit=0.10)
    t2.wave_spike, t2.wave_spike_s, t2.wave_spike_tr = 5.0, 20.0, 1.0
    for k in range(601):
        b = 0.39 + 0.31 * k / 600.0
        ticks_mod.mids.note(HELD, close2 - 1200 + k, b, b + 0.01)
    assert asker(w2, t2, close2)(0.70, 600) is None and not t2.wave_ride
    # z at 0 is off
    t.wave_ride, t.wave_spike = False, 0.0
    assert asker(w, t, close)(0.70, 600) is None and not t.wave_ride


def test_integration_with_no_reading_of_the_bids_typical_minute_the_staircase_sells_nothing(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.50, unit=None)  # no fixed 5c swing on a made-up size
    ask = asker(w, t, close)
    t.wave_best_net, t.wave_dir, t.wave_last_low, t.wave_hi, t.wave_lo = 0.5, -1, 0.35, 0.60, 0.30
    assert ask(0.20, 780) is None
    assert t.wave_dir == -1 and t.wave_last_low == 0.35  # and the state is left as it was


def test_integration_a_gain_smaller_than_a_swing_does_not_arm_the_loss_backup(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.50, unit=0.10)
    t.wave_arm_swings = 1.0
    ask = asker(w, t, close)  # a swing at 780 s left: 0.25 x 0.10 x sqrt(13) = 0.09
    t.wave_best_net, t.wave_dir, t.wave_last_low, t.wave_hi, t.wave_lo = 0.003, -1, 0.49, 0.52, 0.45
    assert ask(0.44, 780) is None  # ahead by 0.3c once: not enough to sell a loss
    t.wave_best_net = 0.12
    t.wave_peak_bid = 0.0
    why = ask(0.43, 779)
    assert why and "last swing low" in why  # ahead by more than a swing: armed


def test_unit_the_time_curve_is_tight_early_widest_from_five_to_three_minutes_and_shorter_after():
    from kalshi_moonshot import wavetop

    c = ((900.0, 0.5), (450.0, 0.5), (300.0, 2.0), (180.0, 2.0), (60.0, 1.0))
    f = lambda left: wavetop.time_factor(c, left, 0.5)
    assert f(900) == f(500) == 0.5 and f(450) == 0.5  # up to mid-market: sells the first fall
    assert f(300) == f(240) == f(180) == 2.0  # five to three minutes: holds
    assert 1.0 < f(120) < 2.0 and f(60) == f(30) == 1.0  # after three minutes: shorter holds
    assert wavetop.time_factor((), 300.0, 0.5) == pytest.approx(5 ** 0.5)  # no curve: the power law as it was


def test_unit_a_lane_file_with_a_time_curve_loads_it_largest_seconds_first(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(TOML.replace("wave_top_trend_hours = 1.0", "wave_top_trend_hours = 1.0\nwave_time_curve = [[60, 1.0], [900, 0.5], [300, 2.0]]\nwave_arm_swings = 0.5"))
    sp = lanes.lane_specs(config.load())["wt"]
    assert sp.wave_time_curve == ((900.0, 0.5), (300.0, 2.0), (60.0, 1.0)) and sp.wave_arm_swings == 0.5


def test_integration_a_sale_at_a_gain_is_armed_by_any_gain_only_the_loss_sale_needs_a_swing(tmp_path, monkeypatch, mem):
    w, t, close = take_target(tmp_path, monkeypatch, 0.50, unit=0.10)
    t.wave_arm_swings = 1.0
    ask = asker(w, t, close)  # a swing at 780 s left: 0.09
    t.wave_best_net, t.wave_dir, t.wave_last_low, t.wave_hi, t.wave_lo = 0.04, -1, 0.56, 0.62, 0.55
    why = ask(0.55, 780)  # ahead by less than a swing, under its last low, still at a gain after both fees: sold
    assert why and "a gain of" in why
