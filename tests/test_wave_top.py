"""The wave-top reversal lane (wavetop.py; 2026-11-21, the user's hand scalp): the detector, the buy step's gates, the salvage sale, and both through the lane file."""
import time

import pytest

from kalshi_moonshot import buychain, config, lanes, memguard, sellchain, wavetop
from kalshi_moonshot import ticks as ticks_mod
from kalshi_moonshot.strategy import LongShot

OPEN = 1_900_000_800.0  # a quarter-hour
INDEX = "BRTI"

TOML = """
[lane.wt]
estimator = "ladder"
needs_model_edge = false
fixed_stake = 7.5
sides = ["yes", "no"]
price = [0.15, 0.45]
hours = [0.0, 0.26]
entry_elapsed = [2, 8]
series = ["KXBTC15M"]
timed_suffix = "15M"
wave_top_quantile = 0.9
buy_module = "m"
exit_module = "x"

[module.m]
steps = ["price", "wave_top"]

[exit.x]
steps = ["wave_salvage"]
curve_risk_live = true

[exit.x.profit]

[exit.x.loss]
"""


def path(direction=1, wave=30.0, back=1.5, bounce=0.6, start=100_000.0, lead=3700, tilt=0.0):
    """One tick a second from 3,700 s before the open: a triangle of 10 every 200 s (a typical minute of 3, legs of slope 1), then, from the open, a wave of `wave` over 150 s in `direction`, 60 s at the top, a move `back` over 30 s and `bounce` the other way over 10 s. Returns (times, values, the second the path ends)."""
    T, V, v = [], [], start
    t0 = int(OPEN) - lead
    for k in range(lead):
        v += (0.05 if (k // 200) % 2 == 0 else -0.05) + tilt  # tilt: a drift over the hours before the open
        T.append(float(t0 + k))
        V.append(v)
    t = int(OPEN)
    for seg, total in ((150, wave * direction), (60, 0.0), (30, -back * direction), (10, bounce * direction)):
        for _ in range(seg):
            v += total / seg
            T.append(float(t))
            V.append(v)
            t += 1
    return T, V, float(t - 1)


@pytest.fixture
def mem():
    ticks_mod.history.clear()
    ticks_mod.mids.clear()
    wavetop._memo.clear()
    yield
    ticks_mod.history.clear()
    ticks_mod.mids.clear()
    wavetop._memo.clear()


def feed(T, V):
    ticks_mod.history.warm(INDEX, list(zip(T, V, strict=True)), T[0] - 1.0)


def spec(tmp_path, monkeypatch, quantile=0.9, trend=1.0):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(TOML.replace("wave_top_quantile = 0.9", f"wave_top_quantile = {quantile}\nwave_top_trend_hours = {trend}"))
    return lanes.lane_specs(config.load())["wt"]


def shot(now, side, ask=0.30):
    left = OPEN + 900.0 - now
    return LongShot("KXBTC15M-30JAN010015-15", "t", "", ask - 0.01, ask, left / 3600.0, "2099-01-01T00:00:00Z", left / 3600.0, lane="wt", series="KXBTC15M", side=side, event_ticker="KXBTC15M-30JAN010015")


def ask_step(sp, sh, now):
    return buychain.STEPS["wave_top"](buychain.Ctx(config.load(), sp, now=now), sh)


# ---- the detector ----
def test_unit_a_wave_bigger_than_the_legs_before_it_that_went_back_and_forth_is_a_top():
    T, V, now = path()
    w, why = wavetop.read(T, V, OPEN, now, 0.9)
    assert w is not None, why
    assert w.d == 1 and w.side == "no" and w.legs >= wavetop.MIN_LEGS and w.slope > w.prev_max and w.slope >= w.cut
    assert w.move >= wavetop.MIN_MOVE and w.back >= wavetop.BACK_U and w.bounce >= wavetop.BOUNCE_U and 2.4 < w.unit < 3.0
    down, why = wavetop.read(*path(direction=-1)[:2], OPEN, now, 0.9)
    assert down is not None and down.d == -1 and down.side == "yes", why


def test_unit_the_wave_history_runs_through_the_open_without_a_cut():
    T, V, _ = path()
    z_legs = wavetop.read(T, V, OPEN, OPEN + 150 + 60 + 30 + 9, 0.9)[0].legs
    assert z_legs >= 15  # the legs of the hour before the open are all there: nothing restarts at the open


def test_unit_no_top_without_the_size_the_stand_or_the_back_and_forth():
    T, V, now = path(wave=4.0)  # no bigger than the legs before it
    assert wavetop.read(T, V, OPEN, now, 0.9)[0] is None
    T, V, now = path(back=0.3, bounce=0.0)  # still at the top
    w, why = wavetop.read(T, V, OPEN, now, 0.9)
    assert w is None and "back and forth" in why
    T, V, now = path()
    w, why = wavetop.read(T, V, OPEN, OPEN + 155, 0.9)  # the top has not stood yet
    assert w is None and ("old" in why or "back" in why)
    w, why = wavetop.read(T[2000:], V[2000:], OPEN, now, 0.9)  # under an hour of history before the open
    assert w is None and "history" in why


def test_unit_a_turn_of_a_whole_typical_minute_ends_the_wave():
    T, V, now = path(back=4.0, bounce=0.5)  # back more than R_TURN: the leg is confirmed and a new one runs
    assert wavetop.read(T, V, OPEN, now, 0.9)[0] is None


# ---- the buy step ----
def test_unit_the_lane_file_names_the_steps_and_the_fields(tmp_path, monkeypatch):
    sp = spec(tmp_path, monkeypatch)
    assert sp.buy_chain == ("price", "wave_top") and sp.sell_chain == ("wave_salvage",) and sp.wave_top_quantile == 0.9 and sp.wave_top_trend_hours == 1.0
    assert "wave_top" in lanes.BUY_STEPS and "wave_salvage" in sellchain.SELL_STEPS and sp.entry_elapsed == (2.0, 8.0)


def test_integration_the_step_buys_the_reversal_side_only_from_memory(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    T, V, now = path()
    feed(T, V)
    with memguard.decision("buy"):  # a SELECT or a Kalshi GET inside raises
        no, yes = ask_step(sp, shot(now, "no"), now), ask_step(sp, shot(now, "yes"), now)
    assert no.ok and "wave top" in no.why and "buys the reversal" in no.why
    assert not yes.ok and "the reversal is NO" in yes.why


def test_integration_a_dip_bet_is_not_taken_after_a_rising_hour_and_is_taken_after_a_falling_one(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    T, V, now = path(direction=-1, lead=4600, tilt=0.004)  # the spot is about 14 higher at the open than an hour before
    assert V[4600] > V[1000]
    feed(T, V)
    rising = ask_step(sp, shot(now, "yes"), now)
    assert not rising.ok and "rising 1 h" in rising.why
    assert ask_step(spec(tmp_path, monkeypatch, trend=0), shot(now, "yes"), now).ok  # no trend check: the dip bet is taken
    ticks_mod.history.clear()
    wavetop._memo.clear()
    T, V, now = path(direction=-1, lead=4600, tilt=-0.004)
    feed(T, V)
    falling = ask_step(spec(tmp_path, monkeypatch), shot(now, "yes"), now)
    assert falling.ok, falling.why
    ticks_mod.history.clear()
    wavetop._memo.clear()
    T, V, now = path(direction=-1)  # the history starts under an hour and ten minutes before the open: the value an hour before is not in memory
    feed(T, V)
    assert "not in memory" in ask_step(spec(tmp_path, monkeypatch), shot(now, "yes"), now).why


def test_integration_no_history_no_quantile_or_no_index_is_a_no(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    T, V, now = path()
    assert "no tick history" in ask_step(sp, shot(now, "no"), now).why
    feed(T, V)
    assert "sets no quantile" in ask_step(spec(tmp_path, monkeypatch, quantile=0), shot(now, "no"), now).why


def test_unit_one_read_is_fast(mem):
    T, V, now = path()
    began = time.perf_counter()
    for _ in range(5):
        wavetop.read(T, V, OPEN, now, 0.9)
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


def test_integration_the_sell_step_sells_a_dipped_bet_and_records_the_resting_sale_once(tmp_path, monkeypatch, mem):
    from kalshi_moonshot import curverisk, watcher
    from tests.test_risk_exit_flow import CLOSE, TICKER, held

    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['wave_salvage']")
    t = w.targets[(TICKER, "risk_only")]
    opened = CLOSE - 900.0
    t.bought_ts = opened + 200.0
    before = [0.80 + 0.02 * ((k // 30) % 2) for k in range(200)]
    bids = before + [0.80 - 0.004 * k for k in range(1, 31)] + [0.68 + 0.004 * k for k in range(1, 31)] + [0.80] * 20
    for k, b in enumerate(bids):
        ticks_mod.mids.note(TICKER, opened + k, b, b + 0.01)
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
    t.entry = 0.39  # the resting sale at entry + 0.40 would fill at this bid: recorded once, nothing sold by it
    w._sell_wave_salvage(t, risk, 0.80, opened + 245, 600.0, 1.0, c)
    assert t.wave_rest_logged
    t.bought_ts = 0.0
    assert w._sell_wave_salvage(t, risk, 0.80, now, CLOSE - now, -0.02, c) is None  # no buy time: nothing is read
