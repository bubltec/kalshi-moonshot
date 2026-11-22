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
    T, V, _ = peak_path()
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
    from tests.test_risk_exit_flow import CLOSE, TICKER as HELD, held

    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['wave_salvage']")
    t = w.targets[(HELD, "risk_only")]
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


# ---- the take sales (wave_take) ----
def test_integration_wave_take_sells_the_resting_forty_and_the_twenty_that_stopped_rising(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import curverisk, watcher
    from tests.test_risk_exit_flow import CLOSE, TICKER as HELD, held

    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['wave_take']")
    t = w.targets[(HELD, "risk_only")]
    t.entry, t.bought_ts = 0.30, CLOSE - 800.0
    risk = curverisk.Risk(0.5, 0.5, 0.5, "price")
    c = watcher._SellWalk(30.0, 0.83, 0.78, None)

    def ask(bid, at):
        with memguard.decision("sell"):
            return w._sell_wave_take(t, risk, bid, at, CLOSE - at, 0.0, c)

    t0 = CLOSE - 800.0
    assert ask(0.35, t0 + 20) is None and ask(0.45, t0 + 40) is None  # +0.15: not yet
    assert ask(0.50, t0 + 60) is None  # +0.20 and a new high: still rising
    assert ask(0.50, t0 + 65) is None  # no new high for 5 s: a quarter of the 60 s run is 15 s
    assert ask(0.49, t0 + 76) is None  # 16 s without a new high but the bid is back under +0.20
    why = ask(0.50, t0 + 77)
    assert why and why.startswith("wave take") and "slowed" in why  # +0.20 and 17 s without a new high (a quarter of the 60 s run is 15 s)
    assert ask(0.60, t0 + 80) is None  # a new high resets the wait
    spike = ask(0.70, t0 + 81)
    assert spike and "resting sale at +0.40" in spike
    t.wave_peak_bid = 0.0
    assert ask(0.44, t0 + 200) is None  # +0.14: neither sale
    t.bought_ts = 0.0
    assert ask(0.80, t0 + 300) is None  # no buy time, nothing is read
