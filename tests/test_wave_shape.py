"""The wave shape (waveshape.py, buy step `wave_shape`; 2026-11-24, PAPER, the classifier of scripts/wave_buy_shape_classifier.py): the broad curve's turn with a base near the target, on a repeated swing (R) or one wide swing (S); NO at a peak, YES at a trough.

Unit tests on synthetic curves (the classifier alone); a replay of the user's labelled wave buys (tests/fixtures/wave_shape_labels.json.gz, built by scripts/wave_shape_fixture.py from the study's own data: the study's verdict on each is the expected one); integration through a lane file -> lane refresh -> the buy chain's decision (buychain.decide and runner.evaluate), memory only, with the decision's note naming the step's readings."""
import functools
import gzip
import json
import random
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kalshi_moonshot import buychain, config, lanes, memguard, waveshape
from kalshi_moonshot import ticks as ticks_mod
from kalshi_moonshot.strategy import LongShot

OPEN = 1_900_000_800.0  # a quarter-hour
INDEX = "BRTI"
K = 1000.0  # the target
TICKER = "KXBTC15M-30JAN010015-15"
U = 7.75  # about the typical minute of the hour's random walk below (1 a second: sqrt(60))


def curve(plan, seed=3, flip=1):
    """An hour of random walk (1 a second) that ends at the target at the open, then (seconds, change in U) segments from the open, straight lines. flip=-1 mirrors the whole curve through the target."""
    rnd = random.Random(seed)
    walk, x = [], 0.0
    for _ in range(waveshape.PRE):
        x += rnd.gauss(0.0, 1.0)
        walk.append(x)
    T = [OPEN - waveshape.PRE + k for k in range(waveshape.PRE)]
    V = [K + flip * (w - walk[-1]) for w in walk]
    v, t = K, int(OPEN)
    for seconds, change in plan:
        for _ in range(seconds):
            v += flip * change * U / seconds
            T.append(float(t))
            V.append(v)
            t += 1
    return T, V


def verdicts(T, V, side, first=61, last=None):
    """{second after the open: Verdict} for every second in [first, last]."""
    last = last if last is not None else int(T[-1] - OPEN)
    g = waveshape.grid(T, V, int(OPEN) - waveshape.PRE, int(T[-1]) + 1)
    out = {}
    for t in range(first, last + 1):
        sh, why = waveshape.shape(g, t)
        assert sh is not None, why
        out[t] = waveshape.judge(sh, side, K)
    return out


def passing(T, V, side, **kw):
    return [t for t, v in verdicts(T, V, side, **kw).items() if v.ok]


# a base at the target, a swing of 2 U up and back (to 0.2 U over it), the same swing again, then a fall: the second peak is at 270 s
REPEATED = [(90, 2.0), (90, -1.8), (90, 1.8), (150, -1.5)]


# ---- the classifier ----
def test_unit_a_repeated_swing_over_the_target_buys_NO_at_the_second_peak_and_never_YES():
    T, V = curve(REPEATED)
    got = passing(T, V, "no")
    assert got and all(255 <= t <= 305 for t in got), got  # at the second peak's turn, not before it and not long after
    v = verdicts(T, V, "no", first=got[0], last=got[0])[got[0]]
    assert v.branch.startswith("R") and 0.75 <= v.f["rep"] <= 1.35 and v.f["cB"] <= 1.0
    assert passing(T, V, "yes") == []
    first_peak = verdicts(T, V, "no", first=92, last=92)[92]  # the first peak: no earlier swing, and small against the hour
    assert not first_peak.ok and "no earlier swing" in first_peak.fails_r and "small vs the hour" in first_peak.fails_s
    assert verdicts(T, V, "no", first=got[-1] + 1, last=got[-1] + 1)[got[-1] + 1].fails_r == ["not at the turn"]  # the broad curve has turned


def test_unit_the_mirror_under_the_target_buys_YES_at_the_second_trough():
    T, V = curve(REPEATED, flip=-1)
    got = passing(T, V, "yes")
    assert got and all(255 <= t <= 305 for t in got), got
    assert passing(T, V, "no") == []
    assert verdicts(T, V, "yes", first=got[0], last=got[0])[got[0]].f["cB"] <= 1.0


def test_unit_mid_move_and_past_the_turn_are_not_bought():
    T, V = curve(REPEATED)
    v = verdicts(T, V, "no", first=240, last=240)[240]  # two thirds of the way up the second rise: the swing so far is too small against the first
    assert not v.ok and "swings not comparable" in v.fails_r
    v = verdicts(T, V, "no", first=330, last=330)[330]  # a minute after the second peak: a third of the swing given back
    assert not v.ok and "not at the turn" in v.fails_r and "not at the turn" in v.fails_s


def test_unit_a_slow_one_way_drift_is_not_bought():
    T, V = curve([(600, 3.0)])  # 0.3 U a minute, straight up off the target
    assert passing(T, V, "no") == [] and passing(T, V, "yes") == []
    v = verdicts(T, V, "no", first=400, last=400)[400]
    assert "slow drift" in v.fails_s and v.fails_r


def test_unit_a_spike_is_not_a_swing():
    T, V = curve([(150, 0.3), (3, 4.0), (3, -4.0), (300, 0.0)])  # 4 U up and back in six seconds: the broad curve does not see it
    assert passing(T, V, "no") == [] and passing(T, V, "yes") == []
    T, V = curve([(150, 0.3), (3, 4.0), (3, -4.0), (90, 0.0), (3, 4.0), (3, -4.0), (200, 0.0)])  # two spikes are not a repeated swing either
    assert passing(T, V, "no") == []


def test_unit_one_wide_fast_swing_off_the_target_buys_on_S_at_its_turn():
    T, V = curve([(120, 0.2), (150, 8.0), (200, -3.0)], seed=5)  # a wide run up off the target, then a turn
    v = verdicts(T, V, "no")
    s_only = [t for t, x in v.items() if x.ok and x.branch == "S"]
    assert s_only, {t: (x.fails_r, x.fails_s) for t, x in list(v.items())[200:300:10]}
    assert all(v[t].f["S_cB"] <= 1.0 and v[t].f["S_speed"] >= 0.6 for t in s_only)
    assert not any(x.ok for t, x in v.items() if t > 330)  # once a third of the swing is given back, no buy


def test_unit_the_first_minute_and_a_flat_hour_are_no_reading_with_a_reason():
    T, V = curve(REPEATED)
    g = waveshape.grid(T, V, int(OPEN) - waveshape.PRE, int(OPEN) + 100)
    sh, why = waveshape.shape(g, 30)
    assert sh is None and "first minute" in why
    flat = [K] * (waveshape.PRE + 200)
    sh, why = waveshape.shape(flat, 150)
    assert sh is None and "did not move" in why
    sh, why = waveshape.shape([*flat[:200], None, *flat[201:]], 150)
    assert sh is None and "does not cover the hour" in why


def test_unit_the_rolling_median_is_the_studys_median():
    rnd = random.Random(11)
    g = [rnd.gauss(0.0, 1.0) for _ in range(800)]
    for h in (15, 30, 120):
        got = waveshape.broad(g, 100, 700, h, 700)
        want = []
        for k in range(100, 701):
            w = sorted(g[max(0, k - h) : min(700, k + h) + 1])
            want.append(waveshape.median_sorted(w))
        assert got == want


def test_unit_one_reading_of_a_full_market_is_fast():
    T, V = curve([(890, 0.0)])
    g = waveshape.grid(T, V, int(OPEN) - waveshape.PRE, int(OPEN) + 890)
    began = time.perf_counter()
    for _ in range(5):
        sh, _ = waveshape.shape(g, 889)
        waveshape.judge(sh, "no", K)
    assert (time.perf_counter() - began) / 5 < 0.03  # half the 60 ms budget of a decision


# ---- the user's labelled buys (the study's data) ----
FIXTURE = Path(__file__).parent / "fixtures" / "wave_shape_labels.json.gz"


def labelled():
    with gzip.open(FIXTURE, "rt") as fh:
        return json.load(fh)


def test_replay_the_users_labelled_buys_get_the_studys_verdict():
    rows = labelled()
    seen = {"Q": 0, "N": 0}
    for r in rows:
        sh, why = waveshape.shape(r["g"], r["t"])
        assert sh is not None, (r["ticker"], why)
        v = waveshape.judge(sh, r["side"], r["K"])
        assert v.branch == r["branch"], (r["ticker"], r["label"], v.branch, r["branch"], v.fails_r, v.fails_s)
        if r["label"] in ("Q", "Q2"):
            assert v.ok, r["ticker"]
            seen["Q"] += 1
        if r["label"] == "N":
            assert not v.ok, r["ticker"]
            seen["N"] += 1
    assert seen == {"Q": 5, "N": 16}  # the study's 5 of 5 qualified pass, 0 of 16 not qualified


# ---- the buy step, through the lane file ----
TOML = """
[lane.ws]
estimator = "market"
needs_model_edge = false
fixed_stake = 5.0
sides = ["yes", "no"]
price = [0.01, 0.99]
hours = [0.0, 0.26]
entry_elapsed = [2, 10]
series = ["KXBTC15M", "KXXRP15M", "KXGOLD15M"]
timed_suffix = "15M"
buy_module = "wave_shape_buy"

[module.wave_shape_buy]
steps = ["entry_window", "wave_shape", "wave_trend"]
"""


@pytest.fixture
def mem():
    def clear():
        ticks_mod.history.clear()
        ticks_mod.mids.clear()
        ticks_mod.strikes._v.clear()
        waveshape._memo.clear()

    clear()
    yield
    clear()


def spec(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(TOML)
    return lanes.lane_specs(config.load())["ws"]


def shot(now, side, ticker=TICKER, series="KXBTC15M", ask=0.30, opened=OPEN):
    left = opened + 900.0 - now
    close = datetime.fromtimestamp(opened + 900.0, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return LongShot(ticker, "t", "", ask - 0.01, ask, left / 3600.0, close, left / 3600.0, lane="ws", series=series, side=side, event_ticker=ticker.rsplit("-", 1)[0])


def feed(T, V, index=INDEX):
    ticks_mod.history.warm(index, list(zip(T, V, strict=True)), T[0] - 1.0)


def test_integration_the_lane_file_loads_the_module_and_its_steps(tmp_path, monkeypatch):
    sp = spec(tmp_path, monkeypatch)
    assert sp.buy_chain == ("entry_window", "wave_shape", "wave_trend") and sp.buy_module == "wave_shape_buy"
    assert "wave_shape" in lanes.BUY_STEPS and "wave_shape" in buychain.STEPS and "wave_shape" not in buychain.NEEDS_ESTIMATE


def test_integration_the_chain_buys_NO_at_the_turn_from_memory_only_and_the_note_names_the_readings(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    T, V = curve(REPEATED)
    feed(T, V)
    ticks_mod.strikes.note(TICKER, K)
    at = passing(T, V, "no")[0]
    now = OPEN + at + 0.5
    with memguard.decision("buy"):  # a SELECT or a Kalshi GET inside raises
        bet_no, note_no = buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "no"))
        bet_yes, note_yes = buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "yes"))
    assert bet_no and not bet_yes
    assert "[chain wave_shape_buy: entry_window > wave_shape > wave_trend |" in note_no
    assert "NO passed every step" in note_no and "wave_shape yes (wave shape R (a repeated swing): W " in note_no
    for reading in ("rho ", "typical minute ", "swing ", "base +", "turn ", "s ago", "buys NO", f"target {K:g}"):
        assert reading in note_no, reading
    assert "YES out at wave_shape" in note_yes and "no wave shape for YES [R: " in note_yes
    later = OPEN + 340.5  # past the turn
    bet, note = buychain.decide(buychain.Ctx(s, sp, now=later), shot(later, "no"))
    assert not bet and "NO out at wave_shape" in note and "not at the turn" in note


def test_integration_the_review_drops_a_side_with_the_steps_reason_for_screen_log(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    T, V = curve(REPEATED)
    feed(T, V)
    ticks_mod.strikes.note(TICKER, K)
    now = OPEN + 240.5  # mid-move
    keep = buychain.screen(buychain.Ctx(s, sp, None, now), [shot(now, "no"), shot(now, "yes")])
    assert keep == []
    reasons = [d["reason"] for d in buychain.DROPPED["ws"]]
    assert any("out at wave_shape" in r and "swings not comparable" in r for r in reasons), reasons


def test_integration_the_entry_window_is_the_lanes_step_and_no_price_band_applies(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    T, V = curve(REPEATED)
    feed(T, V)
    ticks_mod.strikes.note(TICKER, K)
    now = OPEN + passing(T, V, "no")[0] + 0.5
    assert buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "no", ask=0.05))[0]  # any ask: the module lists no price step
    assert buychain.decide(buychain.Ctx(s, sp, now=now), shot(now, "no", ask=0.93))[0]
    late = OPEN + 11 * 60 + 0.5
    bet, note = buychain.decide(buychain.Ctx(s, sp, now=late), shot(late, "no"))
    assert not bet and "NO out at entry_window" in note and "wave_shape yes" not in note and "wave_shape NO" not in note


def test_integration_no_history_no_target_or_no_index_is_a_no(tmp_path, monkeypatch, mem):
    import dataclasses

    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    now = OPEN + 200.5
    ticks_mod.strikes.note(TICKER, K)
    assert "no tick history in memory" in buychain.STEPS["wave_shape"](buychain.Ctx(s, sp, now=now), shot(now, "no")).why
    ticks_mod.strikes._v.clear()
    T, V = curve(REPEATED)
    feed(T, V)
    assert "target is not in memory" in buychain.STEPS["wave_shape"](buychain.Ctx(s, sp, now=now), shot(now, "no")).why
    sh = dataclasses.replace(shot(now, "no"), series="KXNOSUCH15M")
    assert not buychain.STEPS["wave_shape"](buychain.Ctx(s, sp, now=now), sh).ok


def test_integration_both_sides_and_every_lane_in_one_second_compute_it_once_under_the_budget(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    T, V = curve([(890, 0.0)])
    feed(T, V)
    ticks_mod.strikes.note(TICKER, K)
    calls = []
    real = waveshape.shape
    monkeypatch.setattr(waveshape, "shape", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    now = OPEN + 589.5
    began = time.perf_counter()
    with memguard.decision("buy"):
        for side in ("yes", "no", "yes", "no"):
            buychain.STEPS["wave_shape"](buychain.Ctx(s, sp, now=now), shot(now, side))
    assert len(calls) == 1 and time.perf_counter() - began < 0.06


def test_integration_a_labelled_buy_through_the_tick_memory_gets_the_studys_verdict(tmp_path, monkeypatch, mem):
    """XRP 07:30 of 2026-11-23 (qualified, R) and GOLD 08:00 (not qualified): their spot paths fed into the tick memory as one tick a second, the step asked at the buy's second."""
    spec(tmp_path, monkeypatch)
    s = config.load()
    rows = {(r["ticker"].split("-")[0], r["label"]): r for r in labelled()}
    for key, want in ((("KXXRP15M", "Q"), True), (("KXGOLD15M", "N"), False)):
        r = rows[key]
        ticks_mod.history.clear()
        ticks_mod.strikes._v.clear()
        waveshape._memo.clear()
        opened = r["close"] - 900.0
        a = int(opened) - waveshape.PRE
        T = [float(a + k) for k, x in enumerate(r["g"]) if x is not None]
        V = [x for x in r["g"] if x is not None]
        feed(T, V, r["index"])
        if key[0] == "KXXRP15M":
            ticks_mod.strikes.note(r["ticker"], r["K"])
        now = opened + r["t"] + 0.5
        series = key[0]
        with memguard.decision("buy"):
            ok, why = waveshape.admits(s, shot(now, r["side"], ticker=r["ticker"], series=series, opened=opened), now, ticks_mod.strikes.get(r["ticker"]))
        assert ok is want, why
        if want:
            assert why.startswith("wave shape R")


def test_integration_through_the_runners_evaluation_the_decision_note_carries_the_shape(tmp_path, monkeypatch, mem):
    """Lane file -> runner.evaluate (the worker's decision: the lane's module, sizing, the paper bet) at the turn, memory only for the step."""
    from kalshi_moonshot import regime, runner
    from kalshi_moonshot.store import Store
    from tests.decide_support import Fixed

    spec(tmp_path, monkeypatch)
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    T, V = curve(REPEATED)
    feed(T, V)
    ticks_mod.strikes.note(TICKER, K)
    now = OPEN + passing(T, V, "no")[0] + 0.5
    real_ctx = buychain.Ctx
    monkeypatch.setattr(buychain, "Ctx", functools.partial(real_ctx, now=now))
    out = runner.evaluate(s, None, None, store, shot(now, "no"), estimators={"market": Fixed()})
    assert "[chain wave_shape_buy: entry_window > wave_shape > wave_trend |" in out.note and "wave_shape yes (wave shape R" in out.note, out.note
    assert "YES out at wave_shape" in out.note
    later = OPEN + 340.5
    monkeypatch.setattr(buychain, "Ctx", functools.partial(real_ctx, now=later))
    out = runner.evaluate(s, None, None, store, shot(later, "no"), estimators={"market": Fixed()})
    assert out.action == "pass" and "NO out at wave_shape" in out.note and "not at the turn" in out.note, out.note
