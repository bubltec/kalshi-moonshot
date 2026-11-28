"""The low-side buy in a dip or a peak (wavelow.py, buy step `wave_low`; 2026-11-27, user: "Whichever side is low. That's what you buy in the dip or peak").

Unit tests on synthetic curves, the two markets the user corrected (ETH 13:00 ET: the dip the live reader missed; SILVER 11:45 ET: the linear climb it bought NO into), the lane file ->
buy chain decision, the once-per-swing rule, and memory only (memguard)."""
import gzip
import json
from pathlib import Path

import pytest

from kalshi_moonshot import buychain, config, lanes, memguard, wavelow, waveturn
from kalshi_moonshot import ticks as ticks_mod
from kalshi_moonshot.strategy import LongShot
from tests.test_wave_turn import (  # noqa: F401  (feed etc.: the helpers)
    INDEX,
    OPEN,
    TICKER,
    K,
    feed,
    hour_path,
    mem,
    u_of,
    window_path,
)


@pytest.fixture(autouse=True)
def _fresh():
    wavelow.clear()
    yield
    wavelow.clear()


FIXTURE = Path(__file__).parent / "fixtures" / "wave_low_paths.json.gz"

TOML = """
[lane.wl]
estimator = "market"
needs_model_edge = false
fixed_stake = 2.0
sides = ["yes", "no"]
price = [0.18, 0.40]
hours = [0.0, 0.26]
entry_elapsed = [0, 15]
series = ["KXBTC15M"]
timed_suffix = "15M"
buy_module = "wave_low_buy"
record_step_facts = true

[module.wave_low_buy]
steps = ["price", "entry_window", "wave_low"]
"""


def arc_path(plan, u):
    """The window from the open: (seconds, change in u, arc) segments from the target. An arc segment shoots off and rounds out (its pace falls to nothing at its end: the left half of a U, or of its inverse); a straight one keeps its pace (the user's 'linear shape')."""
    v, out = K, [K]
    for seconds, change, arc in plan:
        base = v
        for i in range(1, seconds + 1):
            x = i / seconds
            out.append(base + change * u * ((1 - (1 - x) ** 3) if arc else x))
        v = base + change * u
    return out


def quiet(hour):
    """The pre-open hour with its last 30 minutes flat at the target: the step reads 30 minutes before the decision, and a random-walk hour would put a random swing there (the 'swing before this one' rule reads it)."""
    return hour[:-1800] + [K] * 1800


def spec(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(TOML)
    return lanes.lane_specs(config.load())["wl"]


def shot(now, side, ask=0.30, ticker=TICKER, opened=OPEN, series="KXBTC15M"):
    from datetime import UTC, datetime

    left = opened + 900.0 - now
    close = datetime.fromtimestamp(opened + 900.0, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return LongShot(ticker, "t", "", ask - 0.01, ask, left / 3600.0, close, left / 3600.0, lane="wl", series=series, side=side, event_ticker=ticker.rsplit("-", 1)[0])


class Store:
    """The bet book as the step reads it: the times this lane bought a market and side."""

    def __init__(self, times=()):
        self.times = list(times)

    def lane_bet_times(self, ticker, lane, side):
        return list(self.times)


def ask(sp, s, now, side, store=None, resting=False):
    with memguard.decision("buy"):
        ctx = buychain.Ctx(s, sp, now=now, store=store, resting=resting)
        return buychain.decide(ctx, shot(now, side))


def test_integration_the_lane_file_loads_the_module(tmp_path, monkeypatch):
    sp = spec(tmp_path, monkeypatch)
    assert sp.buy_chain == ("price", "entry_window", "wave_low") and sp.buy_module == "wave_low_buy"
    assert "wave_low" in lanes.BUY_STEPS and "wave_low" in buychain.STEPS and "wave_low" not in buychain.NEEDS_ESTIMATE


def test_the_live_lane_file_names_the_module():
    import tomllib

    raw = tomllib.loads((Path(__file__).parent.parent / "data" / "strategies.toml").read_text()) if (Path(__file__).parent.parent / "data" / "strategies.toml").exists() else None
    if raw is None:
        pytest.skip("data/strategies.toml is untracked and not in this checkout")
    assert raw["lane"]["wave_shape"]["buy_module"] == "wave_low_buy"
    assert raw["module"]["wave_low_buy"]["steps"] == ["price", "entry_window", "wave_low"]


def test_a_fall_is_a_dip_for_yes_and_a_rise_a_peak_for_no(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    u = u_of(hour)
    for plan, want, other in (([(40, 1.5, False), (60, -3.0, True), (300, 0.0, False)], "yes", "no"), ([(40, -1.5, False), (60, 3.0, True), (300, 0.0, False)], "no", "yes")):
        waveturn.clear()
        wavelow.clear()
        ticks_mod.history.clear()
        feed(quiet(hour), arc_path(plan, u))
        ticks_mod.strikes.note(TICKER, K)
        now = OPEN + 40 + 52  # most of the way down (or up) the rounded 3u swing: its pace has nearly gone
        bet, note = ask(sp, s, now, want)
        assert bet, note
        assert "in a dip" in note or "in a peak" in note
        bet, note = ask(sp, s, now, other)
        assert not bet and "not in a" in note, note


def test_a_swing_under_a_typical_minute_is_not_a_dip(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    u = u_of(hour)
    hour = hour[:-720] + [K] * 720  # the last 12 minutes before the open are flat (the step now reads 10 minutes before the decision)
    feed(hour, window_path([(120, 0.0), (60, -0.8), (300, 0.0)], u))
    ticks_mod.strikes.note(TICKER, K)
    bet, note = ask(sp, s, OPEN + 120 + 60, "yes")
    assert not bet and "not yet a dip" in note, note


def test_a_calm_hour_does_not_stop_the_look(tmp_path, monkeypatch, mem):
    """2026-11-28 (user: nothing waits on a typical-minute calculation): a pre-open hour with no completed leg is no veto; the swing is judged on its own."""
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = [K - (waveturn.PRE - 1 - k) * 0.001 for k in range(waveturn.PRE)]  # a straight line: no leg of a typical minute turns in it
    feed(hour, window_path([(120, 0.0), (60, -2.0), (300, 0.0)], u_of(hour)))
    ticks_mod.strikes.note(TICKER, K)
    _bet, note = ask(sp, s, OPEN + 165, "yes")
    assert "not a wave market" not in note and "no reading" not in note, note


def test_no_pre_open_hour_still_reads_the_window(tmp_path, monkeypatch, mem):
    """2026-11-28: after a restart or on a new market there is no hour before the open in memory; the window's own typical minute is read at once, the look is not refused."""
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    u = u_of(hour)
    path = arc_path([(40, 1.5, False), (60, -3.0, True), (300, 0.0, False)], u)
    rows = [(OPEN - 1200 + k, K) for k in range(1200)] + [(OPEN + k, v) for k, v in enumerate(path)]  # only the last 20 minutes before the open and the window are in memory
    ticks_mod.history.warm(INDEX, rows, rows[0][0] - 1.0)
    ticks_mod.strikes.note(TICKER, K)
    _bet, note = ask(sp, s, OPEN + 40 + 52, "yes")
    assert "pre-open hour" not in note and "the tape in memory" not in note, note


def test_the_lane_buys_a_swing_once_and_waits_for_the_next(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    feed(quiet(hour), arc_path([(40, 1.5, False), (60, -3.0, True), (300, 0.0, False)], u_of(hour)))
    ticks_mod.strikes.note(TICKER, K)
    now = OPEN + 92
    assert ask(sp, s, now, "yes", Store())[0]
    bet, note = ask(sp, s, now + 5, "yes", Store([now + 1]))
    assert not bet and "swing_played" in note, note
    assert ask(sp, s, now + 5, "yes", Store([now + 1]), resting=True)[0]  # the resting buy of the same swing stands


def test_a_bet_in_an_earlier_swing_does_not_count_against_the_next_one(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    feed(quiet(hour), arc_path([(40, 1.5, False), (60, -3.0, True), (60, 3.0, True), (60, -5.0, True), (240, 0.0, False)], u_of(hour)))
    ticks_mod.strikes.note(TICKER, K)
    first = OPEN + 92
    assert ask(sp, s, first, "yes", Store())[0]
    second = OPEN + 160 + 52  # in the second fall, rounding off
    assert ask(sp, s, second, "yes", Store([first]))[0]
    bet, note = ask(sp, s, second, "yes", Store([first, second - 2]))
    assert not bet and "swing_played" in note, note


def test_a_resting_buy_is_withdrawn_once_the_swing_has_turned(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    feed(quiet(hour), arc_path([(40, 1.5, False), (60, -3.0, True), (60, 3.0, True), (240, 0.0, False)], u_of(hour)))
    ticks_mod.strikes.note(TICKER, K)
    assert ask(sp, s, OPEN + 92, "yes", resting=True)[0]
    bet, note = ask(sp, s, OPEN + 100 + 55, "yes", resting=True)
    assert not bet and "not in a dip" in note, note


def load_paths():
    with gzip.open(FIXTURE, "rt") as fh:
        return {m["id"]: m for m in json.load(fh)}


def replay(m, side, seconds, sp, s):
    ticks_mod.history.clear()
    waveturn.clear()
    wavelow.clear()
    opened = float(m["opened"])
    feed(m["hour"], m["v"], index=m["index"], opened=opened)
    ticks_mod.strikes.note(m["id"], m["target"])
    out = []
    for t in seconds:
        now = opened + t + 0.5
        series = "KXETH15M" if m["id"].startswith("eth") else "KXSILVER15M"
        ticker = f"{series}-26NOV271300-00"
        ticks_mod.strikes.note(ticker, m["target"])
        with memguard.decision("buy"):
            ctx = buychain.Ctx(s, sp, now=now, store=Store())
            bet, note = buychain.decide(ctx, shot(now, side, ticker=ticker, opened=opened, series=series))
        out.append((t, bet, note))
    return out


def test_replay_eth_1300_the_dip_the_live_reader_missed_is_bought_inside_the_yes_band(tmp_path, monkeypatch, mem):
    """ETH 12:47:07 to 12:47:25 ET (the market opened 12:45): the spot's fall ran 1.4 typical minutes and the YES ask was 0.34 to 0.37. The live reader fired nothing (a turn needs a
    completed swing, which showed 4 s after the bottom, with YES at 0.49). This step says buy YES through the bottom and never NO there."""
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    m = load_paths()["eth-1300"]
    yes = replay(m, "yes", range(108, 160, 2), sp, s)
    no = replay(m, "no", range(108, 150, 2), sp, s)  # through the fall; once the spot bounces 1u the rise is a peak by the step, and the price step (NO at 0.5 and up) is what keeps NO out
    bought = [t for t, bet, _ in yes if bet]
    assert bought and min(bought) <= 145 and max(bought) >= 150, bought  # the fall, from a typical minute run to the bottom at about 12:47:27 (t=147)
    assert not any(bet for _, bet, _ in no)


def test_replay_silver_1145_the_linear_climb_is_not_a_curve_and_is_not_bought(tmp_path, monkeypatch, mem):
    """SILVER 11:34 ET, 2026-11-27, a straight climb the user called linear. The shape test refuses the seconds around the old bet. (It was shadow from 2026-11-28 08:15 to 09:00 ET and bought straight trends: BNB 08:45, XRP 08:45.) Not claimed: the climb has rounded stretches earlier that pass."""
    assert wavelow.HALF_U_LIVE
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    m = load_paths()["silver-1145"]
    no = replay(m, "no", range(240, 285, 3), sp, s)
    assert not any(bet for _, bet, _ in no)
    assert any("no half U" in note or "not at the middle of a half U" in note for _, _, note in no)


def test_a_straight_fall_is_not_a_half_u_and_a_fall_still_at_full_pace_is_not_at_the_middle(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    monkeypatch.setattr(wavelow, "HALF_U_LIVE", True)  # the shape test is shadow by default; this is what it does when it is on
    s = config.load()
    hour = hour_path()
    u = u_of(hour)
    feed(quiet(hour), arc_path([(40, 1.5, False), (60, -3.0, False), (300, 0.0, False)], u))  # the same 2u fall, in a straight line
    ticks_mod.strikes.note(TICKER, K)
    bet, note = ask(sp, s, OPEN + 40 + 50, "yes")
    assert not bet and "no half U" in note and "straight" in note, note
    wavelow.clear()
    ticks_mod.history.clear()
    feed(quiet(hour), arc_path([(40, 1.5, False), (60, -3.0, True), (300, 0.0, False)], u))
    bet, note = ask(sp, s, OPEN + 40 + 30, "yes")  # the rounded fall, 30 s in: past a typical minute and still at full pace
    assert not bet and ("half U" in note), note


def test_with_the_shape_test_off_a_straight_fall_is_bought_and_the_reading_is_stored(tmp_path, monkeypatch, mem):
    """ETH 07:30, 2026-11-28 (user: "still having the same issue where it won't buy the correct side in a dip?"): a sharp V-bottom was refused as straight / still running. With the shape test shadow a straight fall of 3 typical minutes is bought and bow and flat are in the facts."""
    sp = spec(tmp_path, monkeypatch)
    monkeypatch.setattr(wavelow, "HALF_U_LIVE", False)  # the shape test is live by default since 2026-11-28 09:00; this is what shadow mode does
    s = config.load()
    hour = hour_path()
    u = u_of(hour)
    feed(quiet(hour), arc_path([(40, 1.5, False), (60, -3.0, False), (300, 0.0, False)], u))
    ticks_mod.strikes.note(TICKER, K)
    ctx = buychain.Ctx(s, sp, now=OPEN + 40 + 50, store=Store())
    with memguard.decision("buy"):
        bet, note = buychain.decide(ctx, shot(OPEN + 40 + 50, "yes"))
    assert bet and "shape, not judged" in note, note
    w = ctx.facts["wave_low"]
    assert "bow" in w and "flat" in w and w["bow"] < wavelow.HALF_U_BOW  # it would have been refused with the test on


def test_the_swing_is_read_from_ten_minutes_before_the_decision_across_the_open(tmp_path, monkeypatch, mem):
    """2026-11-27 (user: "It's supposed to read at least 10 mins before the marker"; HYPE 22:45, the market opened at the bottom of a 9-minute fall and the step, reading from the open alone, called the rise off it a peak). A fall that runs through the open and rounds off 40 s into the market is one dip, bought as such; read from the open alone it would be a 40 s wobble."""
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    u = u_of(hour)
    # eight minutes of fall into the open, rounding off, then 40 s of the market at the bottom
    top = K + 6.0 * u + 0.002 * u * (1800 - 540)  # the plateau drifts up a hair so its last second is the high, as a real top is
    fall = [K + (top - K) * (1 - i / 540.0) ** 3 for i in range(540)]  # from the high down to the target at the open, flattening
    hour = hour[:-1800] + [K + 6.0 * u + 0.002 * u * i for i in range(1800 - 540)] + fall  # a wavy first half hour, then a high that drifts up, then the fall into the open
    feed(hour, window_path([(60, 0.0)], u))
    ticks_mod.strikes.note(TICKER, K)
    assert wavelow.window_start(int(OPEN), OPEN + 40, wavelow.MIN_LOOKBACK) == int(OPEN) - 560  # 10 minutes before the decision, across the open
    assert wavelow.window_start(int(OPEN), OPEN + 40) == int(OPEN) - 1760  # and 30 when the tape has them
    bet, note = ask(sp, s, OPEN + 40, "yes")
    assert bet and ("in a dip" in note or "in a peak" in note), note
    assert float(next(w for w in [note] if "swing" in w).split("swing ")[1].split("u")[0]) > 2.0, note  # the swing read is the long fall, not the 40 s of the market


def test_with_less_than_ten_minutes_of_tape_the_step_has_no_reading_and_does_not_buy(tmp_path, monkeypatch, mem):
    """Just after a restart the tick memory is short: the curve is not read, and nothing is bought on a window of seconds."""
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    u = u_of(hour)
    plan = [(40, 1.5, False), (60, -3.0, True), (300, 0.0, False)]
    feed(hour[-240:], arc_path(plan, u), opened=OPEN)  # only the last four minutes before the open, then the market
    ticks_mod.strikes.note(TICKER, K)
    bet, note = ask(sp, s, OPEN + 92, "yes")
    assert not bet and "does not reach back" in note, note


def test_a_swing_whose_resting_buy_missed_is_not_bought_again(tmp_path, monkeypatch, mem):
    """XRP 07:15 NO, 2026-11-28 (user: "if this is a resting buy issue where it's trying to buy anyway we should cancel the order instead of it misses"): the first resting buy at 07:05:12 rested 5 s and filled nothing, a second went in 15 s later mid-rise and filled. A miss closes the swing."""
    from kalshi_moonshot import restbuys

    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    feed(quiet(hour), arc_path([(40, 1.5, False), (60, -3.0, True), (300, 0.0, False)], u_of(hour)))
    ticks_mod.strikes.note(TICKER, K)
    restbuys.reset()
    now = OPEN + 92
    assert ask(sp, s, now, "yes", Store())[0]
    restbuys.note_miss(TICKER, "yes", now + 6)  # the resting buy rested its time and filled nothing
    bet, note = ask(sp, s, now + 15, "yes", Store())
    assert not bet and "swing_missed" in note, note
    assert ask(sp, s, now + 15, "yes", Store(), resting=True)[0]  # a re-judge of an order that is still resting is not a new attempt
    restbuys.reset()


def test_a_bounce_off_a_bigger_move_is_not_faded(tmp_path, monkeypatch, mem):
    """BTC 07:15, 2026-11-28 (user: "it was in a dip, above the target, but still need to buy Yes ... In that case this was a no bet"; HYPE 22:45 the same): a 1.2u rise off the bottom of a 4u fall is the way back from a bigger move, not a peak. NO is not bought; a rise off a small wobble still is."""
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    u = u_of(hour)
    # a 4u fall (rounding off at the bottom), then a rising swing that has run 1.5u and is rounding off
    big = [(30, 1.0, False), (80, -4.0, True), (40, 1.5, True), (240, 0.0, False)]
    feed(quiet(hour), arc_path(big, u))
    ticks_mod.strikes.note(TICKER, K)
    bet, note = ask(sp, s, OPEN + 30 + 80 + 38, "no")
    assert not bet and "not fading a recovery" in note, note
    wavelow.clear()
    ticks_mod.history.clear()
    small = [(30, 1.0, False), (80, -0.8, True), (60, 3.0, True), (240, 0.0, False)]  # the swing before the rise was small
    feed(quiet(hour), arc_path(small, u))
    bet, note = ask(sp, s, OPEN + 30 + 80 + 56, "no")
    assert bet and ("in a dip" in note or "in a peak" in note), note


def test_the_price_turn_switch_is_off_by_default_and_never_blocks_an_unread_price(tmp_path, monkeypatch, mem):
    """2026-11-28: PRICE_TURN_LIVE is the user's switch. Off, a dip buys as before. On, a price that is still falling (or flat) blocks a YES; a price not read yet (pc None) never blocks."""
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    u = u_of(hour)
    feed(quiet(hour), arc_path([(40, 1.5, False), (60, -3.0, True), (300, 0.0, False)], u))
    ticks_mod.strikes.note(TICKER, K)
    now = OPEN + 40 + 52
    assert wavelow.PRICE_TURN_LIVE is False
    bet, note = ask(sp, s, now, "yes")
    assert bet, note
    monkeypatch.setattr(wavelow, "PRICE_TURN_LIVE", True)
    monkeypatch.setattr(wavelow, "price_curve", lambda *a, **k: {"pr_dir": -1, "pr_swing_u": 1.0, "pr_gb_u": 0.0, "pr_age": 3, "pr_u": 0.01, "pr_s": 300})
    wavelow.clear()
    bet, note = ask(sp, s, now, "yes")
    assert not bet and "not ticking up" in note, note
    monkeypatch.setattr(wavelow, "price_curve", lambda *a, **k: {"pr_dir": 1, "pr_swing_u": 1.0, "pr_gb_u": 0.0, "pr_age": 3, "pr_u": 0.01, "pr_s": 300})
    wavelow.clear()
    bet, note = ask(sp, s, now, "yes")
    assert bet, note
    monkeypatch.setattr(wavelow, "price_curve", lambda *a, **k: None)
    wavelow.clear()
    bet, note = ask(sp, s, now, "yes")
    assert bet, note  # nothing waits on a price reading that is not there yet
