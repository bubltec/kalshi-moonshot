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
from tests.test_wave_turn import K, OPEN, TICKER, feed, hour_path, u_of, window_path  # noqa: F401  (feed etc.: the helpers)
from tests.test_wave_turn import mem  # noqa: F401  (the fixture)

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
    for plan, want, other in (([(120, 0.0), (60, -2.0), (300, 0.0)], "yes", "no"), ([(120, 0.0), (60, 2.0), (300, 0.0)], "no", "yes")):
        waveturn.clear()
        wavelow.clear()
        ticks_mod.history.clear()
        feed(hour, window_path(plan, u))
        ticks_mod.strikes.note(TICKER, K)
        now = OPEN + 120 + 45  # three quarters of the way down (or up) the 2u swing: it has run 1.5u
        bet, note = ask(sp, s, now, want)
        assert bet, note
        assert "in a dip" in note or "in a peak" in note
        bet, note = ask(sp, s, now, other)
        assert not bet and "not in a" in note, note


def test_a_swing_under_a_typical_minute_is_not_a_dip(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    feed(hour, window_path([(120, 0.0), (60, -0.8), (300, 0.0)], u_of(hour)))
    ticks_mod.strikes.note(TICKER, K)
    bet, note = ask(sp, s, OPEN + 120 + 60, "yes")
    assert not bet and "not yet a dip" in note, note


def test_a_calm_hour_is_not_a_wave_market(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = [K - (waveturn.PRE - 1 - k) * 0.001 for k in range(waveturn.PRE)]  # a straight line: no leg of a typical minute turns in it
    feed(hour, window_path([(120, 0.0), (60, -2.0), (300, 0.0)], u_of(hour)))
    ticks_mod.strikes.note(TICKER, K)
    bet, note = ask(sp, s, OPEN + 165, "yes")
    assert not bet and "not a wave market" in note, note


def test_the_lane_buys_a_swing_once_and_waits_for_the_next(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    feed(hour, window_path([(120, 0.0), (60, -2.0), (300, 0.0)], u_of(hour)))
    ticks_mod.strikes.note(TICKER, K)
    now = OPEN + 165
    assert ask(sp, s, now, "yes", Store())[0]
    bet, note = ask(sp, s, now + 5, "yes", Store([now + 1]))
    assert not bet and "swing_played" in note, note
    assert ask(sp, s, now + 5, "yes", Store([now + 1]), resting=True)[0]  # the resting buy of the same swing stands


def test_a_bet_in_an_earlier_swing_does_not_count_against_the_next_one(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    feed(hour, window_path([(120, 0.0), (60, -2.0), (60, 2.0), (60, -2.5), (240, 0.0)], u_of(hour)))
    ticks_mod.strikes.note(TICKER, K)
    first = OPEN + 165
    assert ask(sp, s, first, "yes", Store())[0]
    second = OPEN + 120 + 60 + 60 + 50  # in the second fall, 2.1u run
    assert ask(sp, s, second, "yes", Store([first]))[0]
    bet, note = ask(sp, s, second, "yes", Store([first, second - 2]))
    assert not bet and "swing_played" in note, note


def test_a_resting_buy_is_withdrawn_once_the_swing_has_turned(tmp_path, monkeypatch, mem):
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    feed(hour, window_path([(120, 0.0), (60, -2.0), (60, 2.0), (240, 0.0)], u_of(hour)))
    ticks_mod.strikes.note(TICKER, K)
    assert ask(sp, s, OPEN + 165, "yes", resting=True)[0]
    bet, note = ask(sp, s, OPEN + 120 + 60 + 55, "yes", resting=True)
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
        left = opened + 900.0 - now
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


def test_replay_silver_1145_a_climb_is_still_read_as_a_peak_known_limit(tmp_path, monkeypatch, mem):
    """SILVER 11:34 ET: a straight climb the user called linear. The market's pre-open hour had waves, so the step reads the rise as a peak and buys NO, as the live reader did.
    Pinned as a KNOWN LIMIT, not a claim of being right: a reading of the window's own shape is not built."""
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    m = load_paths()["silver-1145"]
    no = replay(m, "no", range(240, 275, 5), sp, s)
    assert any(bet for _, bet, _ in no)


def test_the_price_curve_is_a_shadow_reading_in_the_facts_and_changes_no_answer(tmp_path, monkeypatch, mem):
    """2026-11-27 (user: "Using the price is probably better than the spot value for these tight turns"): the market's own YES mid, read with the zig-zag in its own typical minute and a 5 s smoothing, is stored with the step's facts. It never says yes or no."""
    sp = spec(tmp_path, monkeypatch)
    s = config.load()
    hour = hour_path()
    feed(hour, window_path([(120, 0.0), (60, -2.0), (300, 0.0)], u_of(hour)))
    ticks_mod.strikes.note(TICKER, K)
    ticks_mod.mids.clear()
    mid = 0.5
    for k in range(0, 200):
        mid = 0.5 - 0.004 * max(0, k - 120) if k < 180 else mid  # flat, then the YES price falls for 60 s
        ticks_mod.mids.note(TICKER, OPEN + k, mid - 0.005, mid + 0.005)
    now = OPEN + 165
    ctx = buychain.Ctx(s, sp, now=now, store=Store())
    with memguard.decision("buy"):
        bet, note = buychain.decide(ctx, shot(now, "yes"))
    assert bet, note  # the same answer as without the price curve (test_a_fall_is_a_dip_for_yes...)
    w = ctx.facts["wave_low"]
    assert w["pr_dir"] == -1 and w["pr_swing_u"] > 1.0 and w["pr_gb_u"] < 0.2 and w["pr_s"] >= 120 and w["pr_age"] <= 5, w
    ticks_mod.mids.clear()
    wavelow.clear()
    with memguard.decision("buy"):
        ctx2 = buychain.Ctx(s, sp, now=now, store=Store())
        bet2, _ = buychain.decide(ctx2, shot(now, "yes"))
    assert bet2 and "pr_dir" not in ctx2.facts["wave_low"]  # no quotes in memory: no price reading, the buy is unchanged
