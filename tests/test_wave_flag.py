"""The wave flag (waveflag.py, buy step `wave_flag`; 2026-11-23, a candidate beside wave_top, from scripts/wave_flag_lab.py): legs of the raw spot in its own typical 10 s (sigma10), a significant leg's confirmation on one side of the target, NO after a peak, YES after a dip.

Unit tests on synthetic paths (the detector alone); integration through a lane file -> lane refresh -> the buy chain's decision (buychain.decide, and runner.evaluate), memory only, with the decision's note naming the step's reading when it says no and when it says yes. Exact parity with the lab on real windows is scripts/wave_flag_parity.py."""
import dataclasses
import random
import time
from datetime import UTC, datetime

import pytest

from kalshi_moonshot import buychain, config, lanes, memguard, waveflag
from kalshi_moonshot import ticks as ticks_mod
from kalshi_moonshot.strategy import LongShot

OPEN = 1_900_000_800.0  # a quarter-hour
INDEX = "BRTI"
START = 100_000.0
TICKER = "KXBTC15M-30JAN010015-15"
DEFAULTS = {"z_min": 3.0, "turn": 0.35, "m": 1, "window": 180.0}


def background(lead=3700):
    """One tick a second from `lead` s before the open: a triangle of 2.0 every 20 s (slope 0.1 a second). Its legs are confirmed but not significant (z about 1.7)."""
    T, V, v = [], [], START
    t0 = int(OPEN) - lead
    for k in range(lead):
        v += 0.1 if (k // 20) % 2 == 0 else -0.1
        T.append(float(t0 + k))
        V.append(v)
    return T, V, v


def segments(T, V, v, plan):
    """Append (seconds, total change) segments from the open on; a segment of change None continues the triangle."""
    t = int(OPEN)
    for seconds, total in plan:
        for k in range(seconds):
            v += (0.1 if (k // 20) % 2 == 0 else -0.1) if total is None else total / seconds
            T.append(float(t))
            V.append(v)
            t += 1
    return T, V


SPIKE = [(140, None), (20, 20.0), (4, -8.0), (200, 0.0)]  # quiet to 140 s, a rise of 20 over 20 s, a turn of 8 in 4 s (the confirmation, at about 167 s), then flat


def spike_path(direction=1, plan=SPIKE):
    T, V, v = background()
    return segments(T, V, v, [(s, None if c is None else c * direction) for s, c in plan])


def fires(T, V, strike, first=0, last=600, hold=0.0, **kw):
    """[(second after the open, side)] where the flag is up."""
    args = DEFAULTS | kw
    out = []
    for off in range(first, last):
        f, _ = waveflag.read(T, V, OPEN, OPEN + off + 0.5, strike, args["z_min"], args["turn"], args["m"], args["window"], hold)
        if f is not None:
            out.append((off, f.side))
    return out


def why_at(T, V, strike, off, hold=0.0, **kw):
    args = DEFAULTS | kw
    return waveflag.read(T, V, OPEN, OPEN + off + 0.5, strike, args["z_min"], args["turn"], args["m"], args["window"], hold)


# ---- the detector ----
def test_unit_the_unit_is_the_rms_ten_second_change_over_the_hour_before_the_open():
    T, V, _ = background()
    sp = waveflag.grid(T, V, int(OPEN) - waveflag.LEAD, int(OPEN) + 1)
    ch = [sp[x] - sp[x - 10] for x in range(waveflag.LEAD - 3600 + 10, waveflag.LEAD, 10)]
    assert waveflag.sigma10(sp, waveflag.LEAD) == pytest.approx((sum(c * c for c in ch) / len(ch)) ** 0.5)
    assert 0.5 < waveflag.sigma10(sp, waveflag.LEAD) < 1.0


def test_unit_a_quiet_random_looking_path_does_not_fire():
    rnd = random.Random(7)
    T, V, v = [], [], START
    for k in range(3700 + 600):
        v += rnd.gauss(0.0, 1.0 if k < 3400 else 0.5)  # a random walk; from five minutes before the open at half its hour's pace
        T.append(float(int(OPEN) - 3700 + k))
        V.append(v)
    assert fires(T, V, START - 1000.0) == [] and fires(T, V, START + 1000.0) == []
    f, why = why_at(T, V, START - 1000.0, 300, hold=600.0)
    assert f is None and why


def test_unit_a_spike_up_that_turns_over_the_target_fires_NO_at_its_confirmation():
    T, V = spike_path()
    got = fires(T, V, START - 50.0)
    assert got and got[0][1] == "no" and all(side == "no" for _, side in got)
    assert 160 <= got[0][0] <= 170  # the turn of 8 in 4 s, at least 0.35 x the swing of 20, confirms it a few seconds after the top at 160
    assert len(got) == 1  # hold 0: up only in the confirmation second
    f, why = why_at(T, V, START - 50.0, got[0][0])
    assert f.d == 1 and f.leg.z >= 3.0 and f.recent == 1 and "both over the target" in why


def test_unit_the_mirror_dip_under_the_target_fires_YES():
    T, V = spike_path(direction=-1)
    got = fires(T, V, START + 50.0)
    assert got and all(side == "yes" for _, side in got)
    assert "both under the target" in why_at(T, V, START + 50.0, got[0][0])[1]


def test_unit_a_leg_that_crosses_the_target_does_not_fire():
    T, V = spike_path()
    at = fires(T, V, START - 50.0)[0][0]
    strike = START + 10.0  # the rise began under it and ended over it
    assert fires(T, V, strike) == []
    f, why = why_at(T, V, strike, at)
    assert f is None and "did not stay over the target" in why and "z " in why
    assert fires(T, V, START + 100.0) == []  # the whole leg under the target is no peak either


def test_unit_M2_needs_a_second_significant_leg_inside_the_window():
    T, V = spike_path()
    assert fires(T, V, START - 50.0, m=2) == []  # a lone bump
    f, why = why_at(T, V, START - 50.0, fires(T, V, START - 50.0)[0][0], m=2)
    assert f is None and "1 significant leg(s)" in why and "needs 2" in why
    near = [(120, None), (20, 20.0), (4, -7.2), (86, -0.5), (20, 20.0), (4, -8.0), (200, 0.0)]  # two bumps: the second confirmed about 110 s after the first (the fall between reaches its low 90 s after the top: z under 3, not significant)
    T, V = spike_path(plan=near)
    got2 = fires(T, V, START - 50.0, m=2)
    assert len(got2) == 1 and got2[0][1] == "no" and got2[0][0] > 240
    assert len(fires(T, V, START - 50.0, m=1)) == 2
    assert fires(T, V, START - 50.0, m=2, window=100.0) == []  # the same two bumps, counted over a shorter window
    far = [(120, None), (20, 20.0), (4, -7.2), (86, -0.5), (100, 0.0), (20, 20.0), (4, -8.0), (100, 0.0)]  # the second bump about 210 s after the first
    T, V = spike_path(plan=far)
    assert fires(T, V, START - 50.0, m=2) == [] and len(fires(T, V, START - 50.0, m=1)) == 2


def test_unit_a_straight_one_way_run_does_not_fire():
    T, V, v = background()
    T, V = segments(T, V, v, [(100, None), (500, 150.0)])  # a steady climb with no pullback: the leg is never confirmed
    assert fires(T, V, START - 50.0) == []
    f, why = why_at(T, V, START - 50.0, 550)
    assert f is None and "no leg confirmed now" in why


def test_unit_an_unreadable_typical_ten_seconds_is_no_fire_with_a_reason():
    T, V = spike_path()
    late = [(t, v) for t, v in zip(T, V, strict=True) if t >= OPEN - 600]  # the history starts ten minutes before the open
    f, why = waveflag.read([x[0] for x in late], [x[1] for x in late], OPEN, OPEN + 170, START - 50.0, 3.0, 0.35, 1, 180.0)
    assert f is None and "cannot be read" in why
    flat_T = [float(int(OPEN) - 3700 + k) for k in range(3700 + 300)]
    flat_V = [START] * 3700 + [START + 0.1 * k for k in range(300)]
    f, why = waveflag.read(flat_T, flat_V, OPEN, OPEN + 200, START - 50.0, 3.0, 0.35, 1, 180.0)
    assert f is None and "did not move" in why and "cannot be read" in why


def test_unit_the_flag_stays_open_for_the_look_gap_and_no_longer():
    T, V = spike_path()
    at = fires(T, V, START - 50.0)[0][0]
    held = fires(T, V, START - 50.0, hold=5.0)
    assert [o for o, _ in held] == list(range(at, at + 6))  # the confirmation second and the five after it: the entry thread looks once every 5 s
    assert fires(T, V, START - 50.0, first=at + 6, hold=5.0) == []


def test_unit_one_read_of_a_900s_path_is_fast():
    T, V = spike_path(plan=[(900, None)])
    began = time.perf_counter()
    for _ in range(10):
        waveflag.read(T, V, OPEN, OPEN + 899.5, START - 50.0, 3.0, 0.35, 1, 180.0)
    assert (time.perf_counter() - began) / 10 < 0.03  # half the 60 ms budget of a decision


# ---- the buy step, through the lane file ----
TOML = """
[lane.wf]
estimator = "market"
needs_model_edge = false
fixed_stake = 7.5
sides = ["yes", "no"]
price = [0.0, 1.0]
hours = [0.0, 0.26]
entry_elapsed = [2, 10]
series = ["KXBTC15M", "KXGOLD15M"]
timed_suffix = "15M"
buy_module = "flag"

[module.flag]
steps = ["price", "entry_window", "wave_flag"]
"""


@pytest.fixture
def mem():
    def clear():
        ticks_mod.history.clear()
        ticks_mod.mids.clear()
        ticks_mod.strikes._v.clear()
        waveflag._memo.clear()

    clear()
    yield
    clear()


def spec(tmp_path, monkeypatch, extra=""):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(TOML + extra)
    return lanes.lane_specs(config.load())["wf"]


def shot(now, side, ticker=TICKER, series="KXBTC15M", ask=0.30):
    left = OPEN + 900.0 - now
    close = datetime.fromtimestamp(OPEN + 900.0, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return LongShot(ticker, "t", "", ask - 0.01, ask, left / 3600.0, close, left / 3600.0, lane="wf", series=series, side=side, event_ticker=ticker.rsplit("-", 1)[0])


def feed(T, V, index=INDEX):
    ticks_mod.history.warm(index, list(zip(T, V, strict=True)), T[0] - 1.0)


def test_integration_the_lane_file_loads_the_step_and_its_fields_with_the_labs_defaults(tmp_path, monkeypatch):
    sp = spec(tmp_path, monkeypatch)
    assert sp.buy_chain == ("price", "entry_window", "wave_flag") and "wave_flag" in lanes.BUY_STEPS and "wave_flag" in buychain.STEPS
    assert (sp.wave_flag_z, sp.wave_flag_turn, sp.wave_flag_legs, sp.wave_flag_window) == (3.0, 0.35, 1, 180.0)
    text = TOML.replace('buy_module = "flag"', 'buy_module = "flag"\nwave_flag_legs = 2\nwave_flag_window = 120.0')
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(text)
    sp3 = lanes.lane_specs(config.load())["wf"]
    assert sp3.wave_flag_legs == 2 and sp3.wave_flag_window == 120.0
    assert sp3.wave_top_trend_hours == 1.0  # the wave top's own fields are untouched


def test_integration_the_chain_buys_NO_at_the_flag_and_the_note_names_the_reading_from_memory_only(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    T, V = spike_path()
    feed(T, V)
    ticks_mod.strikes.note(TICKER, START - 50.0)
    at = fires(T, V, START - 50.0)[0][0]
    now = OPEN + at + 0.5
    with memguard.decision("buy"):  # a SELECT or a Kalshi GET inside raises
        bet_no, note_no = buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "no"))
        bet_yes, note_yes = buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "yes"))
    assert bet_no and not bet_yes
    assert "[chain flag: price > entry_window > wave_flag |" in note_no
    assert "NO passed every step" in note_no and "wave_flag yes (wave flag: peak: a rise of" in note_no and "buys NO" in note_no and "z " in note_no
    assert "YES out at wave_flag" in note_yes and "the bet is NO, not this side" in note_yes
    later = now + 30.0  # the look gap is over and nothing new was confirmed
    bet, note = buychain.decide(buychain.Ctx(s, sp, now=later), shot(later, "no"))
    assert not bet and "NO out at wave_flag" in note and "no wave flag: no leg confirmed now: the last, a rise of" in note and "s ago" in note


def test_integration_a_dip_buys_YES_and_a_crossing_leg_is_a_no_with_its_reading(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    T, V = spike_path(direction=-1)
    feed(T, V)
    ticks_mod.strikes.note(TICKER, START + 50.0)
    now = OPEN + fires(T, V, START + 50.0)[0][0] + 0.5
    assert buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "yes"))[0]
    waveflag._memo.clear()
    ticks_mod.strikes.note(TICKER, START - 10.0)  # the fall crossed it
    bet, note = buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "yes"))
    assert not bet and "YES out at wave_flag" in note and "did not stay under the target" in note


def test_integration_the_price_and_entry_window_are_the_lanes_steps_not_the_flags(tmp_path, monkeypatch, mem):
    s = config.load()
    T, V = spike_path()
    feed(T, V)
    ticks_mod.strikes.note(TICKER, START - 50.0)
    now = OPEN + fires(T, V, START - 50.0)[0][0] + 0.5
    early = TOML.replace("entry_elapsed = [2, 10]", "entry_elapsed = [5, 10]")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(early)
    sp = lanes.lane_specs(config.load())["wf"]
    bet, note = buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "no"))
    assert not bet and "NO out at entry_window" in note and "wave_flag" not in note.split("|", 1)[1]
    (tmp_path / "strategies.toml").write_text(TOML.replace("price = [0.0, 1.0]", "price = [0.40, 0.55]"))
    sp = lanes.lane_specs(config.load())["wf"]
    assert "NO out at price" in buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "no", ask=0.30))[1]


def test_integration_a_commodity_reads_its_target_as_the_perp_at_the_open_not_kalshis_strike(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import perpref

    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    gold = "KXGOLD15M-30JAN010015-15"
    index = perpref.index_for_series("KXGOLD15M")
    T, V = spike_path(plan=[(130, None), (30, 12.0), (6, -5.0), (20, 0.0), (20, 20.0), (4, -8.0), (200, 0.0)])  # a rise, a pullback that stays well over the open, then the spike
    feed(T, V, index)
    ticks_mod.strikes.note(gold, START + 500.0)  # Kalshi's strike on its own price: off the perp by a basis, here over the whole leg (read against it, nothing would fire)
    assert all(side == "yes" for _, side in fires(T, V, START + 500.0))  # read against Kalshi's strike the peak would be no peak
    at = fires(T, V, V[3700])[-1][0]
    assert fires(T, V, V[3700])[-1][1] == "no"  # the lab's commodity target: the perp's first tick at the open
    now = OPEN + at + 0.5
    bet, note = buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "no", ticker=gold, series="KXGOLD15M"))
    assert bet and f"the target {V[3700]:.6g}" in note
    ticks_mod.history.clear()
    waveflag._memo.clear()
    late = [(t, v) for t, v in zip(T, V, strict=True) if not OPEN <= t < OPEN + 40]  # the perp was not ticking at the open
    feed([x[0] for x in late], [x[1] for x in late], index)
    bet, note = buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "no", ticker=gold, series="KXGOLD15M"))
    assert not bet and "no tick within 25 s of the open" in note


def test_integration_no_history_no_target_or_no_index_is_a_no(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    now = OPEN + 200.5
    ticks_mod.strikes.note(TICKER, START - 50.0)
    assert "no tick history in memory" in buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "no"))[1]
    ticks_mod.strikes._v.clear()
    T, V = spike_path()
    feed(T, V)
    assert "target is not in memory" in buychain.STEPS["wave_flag"](buychain.Ctx(s, sp, now=now), shot(now, "no")).why
    sh = dataclasses.replace(shot(now, "no"), series="KXNOSUCH15M")
    assert not buychain.STEPS["wave_flag"](buychain.Ctx(s, sp, now=now), sh).ok


def test_integration_both_sides_and_every_lane_in_one_second_compute_it_once(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    T, V = spike_path(plan=[(900, None)])
    feed(T, V)
    ticks_mod.strikes.note(TICKER, START - 50.0)
    calls = []
    real = waveflag.read
    monkeypatch.setattr(waveflag, "read", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    now = OPEN + 899.5
    began = time.perf_counter()
    with memguard.decision("buy"):
        for side in ("yes", "no", "yes", "no"):
            buychain.STEPS["wave_flag"](buychain.Ctx(s, sp, now=now), shot(now, side))
    took = time.perf_counter() - began
    assert len(calls) == 1 and took < 0.06


def test_integration_through_the_runners_evaluation_the_decision_note_carries_the_flag(tmp_path, monkeypatch, mem):
    """Lane file -> runner.evaluate (the worker's decision: the lane's module, sizing, the paper bet) at the flag's second, memory only for the step."""
    import functools

    from kalshi_moonshot import regime, runner
    from kalshi_moonshot.store import Store
    from tests.decide_support import Fixed

    spec(tmp_path, monkeypatch)
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    T, V = spike_path()
    feed(T, V)
    ticks_mod.strikes.note(TICKER, START - 50.0)
    at = fires(T, V, START - 50.0)[0][0]
    now = OPEN + at + 0.5
    real_ctx = buychain.Ctx
    monkeypatch.setattr(buychain, "Ctx", functools.partial(real_ctx, now=now))  # the decision's moment: the synthetic market's second
    out = runner.evaluate(s, None, None, store, shot(now, "no"), estimators={"market": Fixed()})
    assert "[chain flag: price > entry_window > wave_flag |" in out.note and "wave_flag yes (wave flag: peak" in out.note, out.note
    assert "YES out at wave_flag" in out.note
    later = now + 60.0
    monkeypatch.setattr(buychain, "Ctx", functools.partial(real_ctx, now=later))
    out = runner.evaluate(s, None, None, store, shot(later, "no"), estimators={"market": Fixed()})
    assert out.action == "pass" and "NO out at wave_flag" in out.note and "no leg confirmed now" in out.note, out.note
