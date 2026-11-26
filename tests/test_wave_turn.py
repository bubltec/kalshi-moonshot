"""The wave turn (waveturn.py, buy step `wave_turn`; 2026-11-24, PAPER, the rule of the study on the user's own marked buys, scripts/wave_turn_chop.py).

Unit tests on synthetic curves; a replay of the study's 17 marked markets (tests/fixtures/wave_turn_marks.json.gz, built by
scripts/wave_turn_fixture.py) through the step's own memory path, which must reproduce the study's counts; integration through a lane file -> lane
refresh -> the buy chain's decision (buychain.decide and runner.evaluate), memory only."""
import functools
import gzip
import json
import random
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kalshi_moonshot import binaryrisk, buychain, config, lanes, memguard, waveturn
from kalshi_moonshot import ticks as ticks_mod
from kalshi_moonshot.strategy import LongShot

OPEN = 1_900_000_800.0
INDEX = "BRTI"
TICKER = "KXBTC15M-30JAN010015-15"
K = 1000.0


@pytest.fixture
def mem():
    def clear():
        ticks_mod.history.clear()
        ticks_mod.mids.clear()
        ticks_mod.strikes._v.clear()
        waveturn.clear()

    clear()
    yield
    clear()


def hour_path(choppy=True, seed=3):
    """An hour before the open ending at the target: choppy = a random walk (crosses each window's open often); calm = a slow one-way drift with tiny noise."""
    rnd = random.Random(seed)
    xs, x = [], 0.0
    for _k in range(waveturn.PRE):
        x += rnd.gauss(0.0, 1.0) if choppy else 0.05 + rnd.gauss(0.0, 0.01)
        xs.append(x)
    return [K + v - xs[-1] for v in xs]


def window_path(plan, u):
    """The window from the open: (seconds, change in u) straight segments starting at the target."""
    v, out = K, [K]
    for seconds, change in plan:
        for _ in range(seconds):
            v += change * u / seconds
            out.append(v)
    return out


def feed(hour, window, index=INDEX, opened=OPEN):
    T = [opened - waveturn.PRE + k for k in range(len(hour))] + [opened + k for k in range(len(window))]
    V = list(hour) + list(window)
    rows = [(t, v) for t, v in zip(T, V, strict=True) if v is not None]
    ticks_mod.history.warm(index, rows, rows[0][0] - 1.0)


def fires(index=INDEX, opened=OPEN, target=K, last=840):
    out = []
    for t in range(60, last + 1):
        side, why, _r, _h = waveturn.read_index(index, opened, opened + t + 0.5, target, 0.0)
        if side is not None and why == "fired":
            out.append((t, side))
    return out


# a swing up of 2 u, back down 2 u, up 2 u, down 2 u (each 60 s), from the target
REPEATED = [(60, 2.0), (60, -2.0), (60, 2.0), (60, -2.0), (300, 0.0)]
LONG = [(60, 2.0), (60, -2.0), (60, 2.0), (60, -2.0), (60, 2.0), (60, -2.0), (300, 0.0)]


def u_of(hour):
    return waveturn.hour_of(hour).u


# ---- unit ----
def test_unit_a_repeated_swing_fires_YES_after_a_dip_and_NO_after_a_peak_once_two_swings_are_complete(mem):
    hour = hour_path()
    u = u_of(hour)
    feed(hour, window_path(LONG, u))
    got = fires(last=400)
    assert got == [(141, "yes"), (201, "no"), (261, "yes"), (321, "no")], got  # the turn at 81 s comes before the first completed swing (about 90 s); one swing is enough before minute 5
    assert not [t for t, _ in got if t < 120]
    side, why, r, _ = waveturn.read_index(INDEX, OPEN, OPEN + 81.5, K, 0.0)  # the first peak's turn: refused for want of completed swings
    assert side is None and "completed swings" in why and r.done == 0
    side, why, r, _ = waveturn.read_index(INDEX, OPEN, OPEN + 201.5, K, 0.0)
    assert side == "no" and why == "fired" and r.done >= waveturn.need(r.t)


def test_unit_the_note_carries_the_leg_time_and_the_room_left_as_shadow(mem):
    hour = hour_path()
    feed(hour, window_path(LONG, u_of(hour)))
    _side, _why, r, h = waveturn.read_index(INDEX, OPEN, OPEN + 250.5, K, 0.0)
    assert 50 <= r.leg_s <= 70 and 1.0 < r.leg_u <= 2.0  # legs of 60 s and 2 u, a little less after the 30 s smoothing
    told = waveturn.note(r, h, K)
    assert "legs: median" in told and "legs left" in told and "(shadow)" in told
    early = waveturn.read_index(INDEX, OPEN, OPEN + 70.5, K, 0.0)[2]
    assert "no completed leg yet" in waveturn.room(early)


def test_unit_the_swings_needed_grow_with_the_minutes_into_the_market():
    assert [waveturn.need(m * 60.0) for m in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12)] == [1, 1, 1, 1, 1, 2, 2, 3, 3, 4, 5]


def test_unit_a_resting_order_stands_on_its_fire_until_the_curve_passes_the_swing_extreme(mem):
    hour = hour_path()
    u = u_of(hour)
    feed(hour, window_path(LONG, u))
    t_fire, side = next((t, sd) for t, sd in fires(last=400) if t >= 190)  # the NO at 201 s, after a peak at 180 s
    waveturn.clear()
    feed(hour, window_path(LONG, u))
    assert waveturn.read_index(INDEX, OPEN, OPEN + t_fire + 0.5, K, 0.0)[:2] == (side, "fired")
    later = t_fire + 20
    entry = waveturn.read_index(INDEX, OPEN, OPEN + later + 0.5, K, 0.0)  # a new entry: the swing has had its buy
    assert entry[0] is None, entry  # the swing has had its buy: no second entry, whatever the reason said (once per swing, or not turned yet)
    stands = waveturn.read_index(INDEX, OPEN, OPEN + later + 0.5, K, 0.0, side)  # the resting order's re-ask: its turn stands
    assert stands[0] == side and "its order stands" in stands[1]
    # the curve climbs back past the peak it turned from: a new high, the turn failed, the order is out
    waveturn.clear()
    ticks_mod.history.clear()
    feed(hour, window_path([*LONG[:4], (120, 4.0)], u))
    fire_t = next(t for t, sd in fires(last=400) if sd == side and t >= 190)
    waveturn.clear()
    feed(hour, window_path([*LONG[:4], (120, 4.0)], u))
    assert waveturn.read_index(INDEX, OPEN, OPEN + fire_t + 0.5, K, 0.0)[0] == side
    assert waveturn.read_index(INDEX, OPEN, OPEN + 270.5, K, 0.0, side)[0] == side  # still under the 2 u peak it turned from
    out = waveturn.read_index(INDEX, OPEN, OPEN + 330.5, K, 0.0, side)  # 3 u: past it
    assert out[0] is None and "passed the swing's extreme" in out[1], out


def test_unit_a_one_way_drift_does_not_fire(mem):
    hour = hour_path()
    u = u_of(hour)
    feed(hour, window_path([(600, 6.0)], u))
    assert fires() == []
    side, why, r, _ = waveturn.read_index(INDEX, OPEN, OPEN + 300.5, K, 0.0)
    assert side is None and "given back" in why and r.gb_u < waveturn.GB


# a trend with small pull-backs: up 3 u in two minutes, back 0.6 u (a turn of 0.5 u: the old rule fires on it), then on up. No swing of 1 u back is ever made.
TREND = [(120, 3.0), (30, -0.6), (120, 3.0), (30, -0.6), (300, 1.0)]


def test_unit_a_trend_with_small_pull_backs_is_refused_for_want_of_a_completed_swing(mem, tmp_path, monkeypatch):
    hour = hour_path()
    u = u_of(hour)
    feed(hour, window_path(TREND, u))
    spec(tmp_path, monkeypatch)
    pulled = [t for t in range(125, 200) if waveturn.read_index(INDEX, OPEN, OPEN + t + 0.5, K, 0.0)[1].startswith("0 completed swings")]
    assert pulled and fires(last=600) == []  # the turns are there (0.5 u swings given back 0.2 u), the gate refuses every one
    now = OPEN + pulled[0] + 0.5
    waveturn.clear()
    feed(hour, window_path(TREND, u))
    ok, why = waveturn.admits(config.load(), shot(now, "no"), now, K)
    assert not ok and "0 completed swings" in why and "a trend has a pull-back at most" in why and "completed swings of 1u 0 (gate >= 1)" in why


def test_unit_the_pre_open_hour_no_longer_gates_it(mem):
    hour = hour_path(choppy=False)  # a calm, one-way hour before the open
    h = waveturn.hour_of(hour)
    assert h.cross is not None and h.cross < 8
    feed(hour, window_path(REPEATED, h.u))
    assert fires(last=300)  # waves in the window are what count; the hour only gives u and the shadow readings


def test_unit_the_side_gate_refuses_a_NO_turn_far_under_the_target(mem):
    hour = hour_path()
    u = u_of(hour)
    feed(hour, window_path([(60, -4.0), (60, 2.0), (60, -2.0), (300, 0.0)], u))  # the whole window under the target: the up-swings' turns are NO turns more than 0.5 u under it
    got = fires(last=240)
    assert all(s == "yes" for _, s in got), got
    reasons = {waveturn.read_index(INDEX, OPEN, OPEN + t + 0.5, K, 0.0)[1] for t in range(150, 190)}
    assert any("more than 0.5u under it" in r for r in reasons)


def test_unit_no_pre_open_hour_in_memory_is_no_reading(mem):
    window = window_path(REPEATED, 1.0)
    ticks_mod.history.warm(INDEX, [(OPEN + k, v) for k, v in enumerate(window)], OPEN - 1.0)
    side, why, r, _h = waveturn.read_index(INDEX, OPEN, OPEN + 100.5, K, 0.0)
    assert side is None and r is None and "no reading of the pre-open hour" in why


def test_unit_once_per_swing_and_held_for_the_look_gap(mem):
    hour = hour_path()
    feed(hour, window_path(REPEATED, u_of(hour)))
    first, first_side = fires(last=300)[0]
    waveturn.clear()
    feed(hour, window_path(REPEATED, u_of(hour)))
    said = [waveturn.read_index(INDEX, OPEN, OPEN + t + 0.5, K, 5.0)[0] for t in range(60, first + 20)]
    yes_at = [60 + i for i, s in enumerate(said) if s == first_side]
    assert yes_at == list(range(first, first + 6))  # the fire second and the 5 s look gap after it, then this swing has had its buy


# ---- replay of the study's marked markets ----
FIXTURE = Path(__file__).parent / "fixtures" / "wave_turn_marks.json.gz"


def replay(m):
    waveturn.clear()
    ticks_mod.history.clear()
    index = binaryrisk.curve_index(m["ticker"].split("-")[0])
    feed(m["hour"], m["v"], index=index)
    return fires(index=index, target=m["target"])


def counts(rows):
    hits = extra = 0
    silent = 0
    for m in rows:
        f = replay(m)
        if m["nogood"]:
            silent += not f
            continue
        used = set()
        for t in m["buy"]:
            side = "no" if m["v"][t] > m["target"] else "yes"
            near = [x for x in f if x[1] == side and abs(x[0] - t) <= 30 and x not in used]
            if near:
                used.add(min(near, key=lambda x: abs(x[0] - t)))
                hits += 1
        extra += len(f) - len(used)
    return hits, silent, extra


def test_replay_the_study_counts_on_the_users_marks(mem):
    with gzip.open(FIXTURE, "rt") as fh:
        rows = json.load(fh)
    assert len(rows) == 17 and sum(len(m["buy"]) for m in rows) == 18
    # Measured 2026-11-25 with the in-market gate (need(t) completed swings of 1u, growing with the minute), chosen AFTER seeing the user's 45 marks (these 17 and 28 more): 6 of the 18
    # marked buys are fired within 30 s, 6 of the 9 no-good markets stay silent, 14 other fires. The old pre-open gate on this first set was 9 / 8 / 27 (the
    # study's own counts were 12 / 0 / 34 with no gate). This is a pin of the code's behaviour on these marks, not a claim that it is good: the 45-market
    # study is studies/results/2026-11-25-wave-swing-gate.txt and forward paper scoring is the test. The PLATINUM 18:30 path lacks the first 310 s of its
    # pre-open hour; the step says 'no reading' for an hour not fully in memory and stays silent there.
    assert counts(rows) == (6, 6, 14)


# ---- the buy step, through the lane file ----
TOML = """
[lane.wt]
estimator = "market"
needs_model_edge = false
fixed_stake = 5.0
sides = ["yes", "no"]
price = [0.01, 0.99]
hours = [0.0, 0.26]
entry_elapsed = [2, 10]
series = ["KXBTC15M"]
timed_suffix = "15M"
buy_module = "wave_turn_buy"

[module.wave_turn_buy]
steps = ["entry_window", "wave_turn", "wave_trend"]
"""


def spec(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(TOML)
    return lanes.lane_specs(config.load())["wt"]


def shot(now, side, ask=0.30):
    left = OPEN + 900.0 - now
    close = datetime.fromtimestamp(OPEN + 900.0, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return LongShot(TICKER, "t", "", ask - 0.01, ask, left / 3600.0, close, left / 3600.0, lane="wt", series="KXBTC15M", side=side, event_ticker=TICKER.rsplit("-", 1)[0])


LATE = [(120, 0.0), (60, 2.0), (60, -2.0), (60, 2.0), (60, -2.0), (300, 0.0)]  # the swings start at minute 2, inside the lane's entry window


def test_integration_the_lane_file_loads_the_module(tmp_path, monkeypatch):
    sp = spec(tmp_path, monkeypatch)
    assert sp.buy_chain == ("entry_window", "wave_turn", "wave_trend") and sp.buy_module == "wave_turn_buy"
    assert "wave_turn" in lanes.BUY_STEPS and "wave_turn" in buychain.STEPS and "wave_turn" not in buychain.NEEDS_ESTIMATE


def test_integration_the_chain_buys_at_the_turn_from_memory_only_and_notes_the_readings(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    window = window_path(LATE, u_of(hour))
    feed(hour, window)
    ticks_mod.strikes.note(TICKER, K)
    t_fire, side = next((t, sd) for t, sd in fires() if t >= 120)
    waveturn.clear()
    feed(hour, window)
    now = OPEN + t_fire + 0.5
    with memguard.decision("buy"):
        bet, note = buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, side))
    assert bet, note
    assert "[chain wave_turn_buy: entry_window > wave_turn > wave_trend |" in note
    for reading in ("wave turn (fired): buys", "crossings of the window's open", "efficiency", "completed swings of 1u", "waves so far", "given back", "from the target", "last-5-min efficiency"):
        assert reading in note, reading
    other = "yes" if side == "no" else "no"
    assert f"{other.upper()} out at wave_turn" in note and "not this side" in note
    later = now + 40.0
    bet, note = buychain.decide(buychain.Ctx(s, sp, now=later), shot(later, side))
    assert not bet and "out at wave_turn" in note


def test_integration_a_resting_buy_from_a_fire_survives_the_per_quote_recheck_and_a_new_entry_does_not_refire(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import restcheck

    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    window = window_path(LATE, u_of(hour))
    feed(hour, window)
    ticks_mod.strikes.note(TICKER, K)
    t_fire, side = next((t, sd) for t, sd in fires() if t >= 120)
    waveturn.clear()
    feed(hour, window)
    now = OPEN + t_fire + 0.5
    assert buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, side))[0]  # the entry: the turn fires, the resting buy is placed
    later = now + 6.0
    v = restcheck.recheck(s, sp, shot(later, side), 0.29, 0.28, 0.30, later)
    assert v.action != "cancel", v  # the per-quote judge keeps it: its turn stands
    assert not buychain.decide(buychain.Ctx(s, sp, now=later), shot(later, side))[0]  # a new entry on the same swing is still refused


def test_integration_the_review_drops_a_gated_market_with_the_reason_for_screen_log(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    u = u_of(hour)
    feed(hour, window_path(TREND, u))
    ticks_mod.strikes.note(TICKER, K)
    t_pull = next(t for t in range(125, 200) if waveturn.read_index(INDEX, OPEN, OPEN + t + 0.5, K, 0.0)[1].startswith("0 completed swings"))
    now = OPEN + t_pull + 0.5
    assert buychain.screen(buychain.Ctx(s, sp, None, now), [shot(now, "no"), shot(now, "yes")]) == []
    assert any("out at wave_turn" in d["reason"] and "0 completed swings" in d["reason"] for d in buychain.DROPPED["wt"])


def test_integration_through_the_runners_evaluation(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import regime, runner
    from kalshi_moonshot.store import Store
    from tests.decide_support import Fixed

    spec(tmp_path, monkeypatch)
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    hour = hour_path()
    window = window_path(LATE, u_of(hour))
    feed(hour, window)
    ticks_mod.strikes.note(TICKER, K)
    t_fire, side = next((t, sd) for t, sd in fires() if t >= 120)
    waveturn.clear()
    feed(hour, window)
    now = OPEN + t_fire + 0.5
    real_ctx = buychain.Ctx
    monkeypatch.setattr(buychain, "Ctx", functools.partial(real_ctx, now=now))
    out = runner.evaluate(s, None, None, store, shot(now, side), estimators={"market": Fixed()})
    assert "[chain wave_turn_buy: entry_window > wave_turn > wave_trend |" in out.note and "wave_turn yes (wave turn (fired)" in out.note, out.note


def test_integration_one_tick_is_fast_and_memory_only(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    feed(hour, window_path([(890, 0.0)], u_of(hour)))
    ticks_mod.strikes.note(TICKER, K)
    began = time.perf_counter()
    with memguard.decision("buy"):
        for side in ("yes", "no"):
            buychain.STEPS["wave_turn"](buychain.Ctx(s, sp, now=OPEN + 600.5), shot(OPEN + 600.5, side))
    assert time.perf_counter() - began < 0.06
