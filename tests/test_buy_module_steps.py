"""The buy rules the live commodity lanes had as lane settings, now steps of a buy module (2026-11-19, user: "we don't use anything outside modules for buying and selling").

Unit: each step answers yes or no on a stubbed reading. Integration: the real lane file's live lanes load with their module's steps and carry no buy or sell setting outside the modules; a lane with no exit module sells nothing."""
import dataclasses
import time
from pathlib import Path

import pytest

from kalshi_moonshot import (
    buychain,
    config,
    curverisk,
    lanes,
    runner,
    ticks,
    variants,
    volregime,
    watcher,
)
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot

TICKER = "KXGOLD15M-30JAN010015-15"


def spec(**kw):
    return lanes.LaneSpec("x", "ladder", "x", (0.60, 0.92), (0, 1), 0.05, 0.0, sides=("yes", "no"), **kw)


def shot(side="yes", ask=0.80):
    return LongShot(TICKER, "t", "", ask - 0.01, ask, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="x", series="KXGOLD15M", side=side, event_ticker="KXGOLD15M-30JAN010015")


def ask(step, sp, sh=None, **ctx):
    s = config.load()
    return buychain.STEPS[step](buychain.Ctx(s, sp, **ctx), sh or shot())


def test_unit_every_step_here_is_named_in_the_lanes_list_and_the_other_way_round():
    assert set(buychain.STEPS) == set(lanes.BUY_STEPS)
    assert {"vol_regime", "cushion", "spike", "center_agree", "same_side", "price_trend", "state_record", "curve_model"} <= set(buychain.STEPS)


def test_unit_no_curve_variety_is_refused_the_letter_is_a_reading_in_the_cushion_note(monkeypatch):
    assert "variety" not in lanes.BUY_STEPS and "variety" not in buychain.STEPS and not hasattr(spec(), "skip_varieties") and not hasattr(variants, "skipped")
    for letter in ("A", "G"):  # 2026-11-21: the oscillating variety is bought like any other
        monkeypatch.setattr(variants, "snapshot_now", lambda *a, _l=letter, **k: {"z": 1.0, "crosses": 6, "variety": _l})
        for side in ("yes", "no"):
            got = ask("cushion", spec(min_cushion_z=0.8), shot(side))
            assert got.ok is True and f"variety {letter}" in got.why


def test_integration_a_lane_file_with_the_variety_step_and_skip_list_loads_without_them(tmp_path, monkeypatch, caplog):
    """Lane file -> module -> spec: the retired step and setting are dropped and logged, the other steps keep their order."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    text = "[module.m]\nsteps=['price','variety','cushion','cooldown']\nmin_cushion_z=0.8\nskip_varieties=['G-no','A-yes']\n\n[lane.a]\nestimator='market'\nprice=[0.60,0.92]\nhours=[0.0,1.0]\nseries=['KXGOLD15M']\nmax_spread=0.05\nmin_volume=0\nbuy_module='m'\n"
    with caplog.at_level("WARNING", logger="moonshot"):
        sp = lanes.lane_specs_from(config.load(), text)["a"]
    assert sp.buy_chain == ("price", "cushion", "cooldown") and "skip_varieties" in caplog.text and "variety" in caplog.text


def test_unit_vol_regime_wants_a_known_regime_under_the_ceiling(monkeypatch):
    sp = spec(max_vol_rel=1.6)
    coin = variants.coin_of("KXGOLD15M")
    monkeypatch.setattr(volregime, "regime", volregime.VolRegime())
    assert "unknown" in ask("vol_regime", sp).why and ask("vol_regime", sp).ok is False
    volregime.regime.set(coin, 1.2, 1.0)
    assert ask("vol_regime", sp).ok is True
    volregime.regime.set(coin, 1.7, 1.0)
    assert ask("vol_regime", sp).ok is False and ask("vol_regime", spec()).ok is False


def test_unit_cushion_wants_a_cushion_and_does_not_limit_crossings(monkeypatch):
    sp = spec(min_cushion_z=0.8)
    monkeypatch.setattr(variants, "snapshot_now", lambda *a, **k: {"z": 1.0, "crosses": 2})
    assert ask("cushion", sp).ok is True
    monkeypatch.setattr(variants, "snapshot_now", lambda *a, **k: {"z": 0.5, "crosses": 2})
    assert ask("cushion", sp).ok is False
    monkeypatch.setattr(variants, "snapshot_now", lambda *a, **k: {"z": 1.0, "crosses": 14})
    got = ask("cushion", sp)
    assert got.ok is True and "14 crossings" in got.why  # 2026-11-21: the crossings are a reading in the note, never a refusal
    monkeypatch.setattr(variants, "snapshot_now", lambda *a, **k: None)
    assert ask("cushion", sp).ok is False and ask("cushion", spec()).ok is False
    assert not hasattr(sp, "max_spot_crosses")


def test_integration_a_lane_file_that_still_sets_the_crossings_limit_loads_without_it(tmp_path, monkeypatch, caplog):
    """Lane file -> module -> spec: the retired setting is dropped and logged, the cushion step still gates on the cushion, and an unknown setting is still refused."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    text = "[module.m]\nsteps=['price','cushion']\nmin_cushion_z=0.8\nmax_spot_crosses=4\n\n[lane.a]\nestimator='market'\nprice=[0.60,0.92]\nhours=[0.0,1.0]\nseries=['KXGOLD15M']\nmax_spread=0.05\nmin_volume=0\nbuy_module='m'\n"
    with caplog.at_level("WARNING", logger="moonshot"):
        sp = lanes.lane_specs_from(config.load(), text)["a"]
    assert sp.min_cushion_z == 0.8 and "max_spot_crosses" in caplog.text
    monkeypatch.setattr(variants, "snapshot_now", lambda *a, **k: {"z": 1.0, "crosses": 9})
    assert ask("cushion", sp).ok is True
    with pytest.raises(ValueError):
        lanes.lane_specs_from(config.load(), text.replace("max_spot_crosses", "no_such_setting"))


def test_unit_spike_refuses_a_side_that_just_ran(monkeypatch):
    sp = spec(entry_spike_seconds=90)
    monkeypatch.setattr(runner, "side_spike", lambda *a, **k: (0.05, 0.10))
    assert ask("spike", sp).ok is True
    monkeypatch.setattr(runner, "side_spike", lambda *a, **k: (0.31, 0.10))
    assert ask("spike", sp).ok is False
    monkeypatch.setattr(runner, "side_spike", lambda *a, **k: (None, None))
    assert ask("spike", sp).ok is True and ask("spike", spec()).ok is False


def test_unit_price_trend_wants_the_side_not_to_have_fallen(monkeypatch):
    sp = spec(entry_trend_seconds=60)
    monkeypatch.setattr(runner, "side_trend", lambda *a, **k: 0.01)
    assert ask("price_trend", sp).ok is True
    monkeypatch.setattr(runner, "side_trend", lambda *a, **k: -0.02)
    assert ask("price_trend", sp).ok is False
    monkeypatch.setattr(runner, "side_trend", lambda *a, **k: None)
    assert ask("price_trend", sp).ok is False and ask("price_trend", spec()).ok is False


def test_unit_center_agree_needs_the_spot_and_the_center_on_the_sides_side_of_the_strike(monkeypatch):
    sp = spec(require_spot_center_agree=True)
    monkeypatch.setattr(ticks.strikes, "get", lambda ticker: 100.0)
    why_up, why_split = "spot 101.0 (center 100.5)", "spot 101.0 (center 99.0)"
    assert ask("center_agree", sp, p_yes=0.9, why=why_up).ok is True
    assert ask("center_agree", sp, shot("no"), p_yes=0.9, why=why_up).ok is False
    assert ask("center_agree", sp, p_yes=0.9, why=why_split).ok is False
    assert ask("center_agree", sp, p_yes=0.9, why="no pair here").ok is True  # a note with no pair is not held to it
    assert ask("center_agree", spec(), p_yes=0.9, why=why_up).ok is False


def test_unit_same_side_counts_the_lanes_open_bets_in_the_window(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    store = Store(config.load().db_path)
    for k, tk in enumerate(("KXGOLD15M-30JAN010015-15", "KXSILVER15M-30JAN010015-20")):
        store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)", (time.time() - k, tk, "bet", "observe", 0.8, "yes", "x", 1, 1, "2099-01-01T00:00:00Z"))
    store.db.commit()
    sp = spec(max_same_side=2)
    assert ask("same_side", sp, shot("no", 0.2), p_yes=0.9, store=store).ok is True  # the open bets are YES
    other = dataclasses.replace(shot(), ticker="KXPLATINUM15M-30JAN010015-9", series="KXPLATINUM15M")
    assert ask("same_side", sp, other, p_yes=0.9, store=store).ok is False
    assert ask("same_side", spec(max_same_side=3), other, p_yes=0.9, store=store).ok is True
    assert ask("same_side", spec(), other, p_yes=0.9, store=store).ok is False


def test_unit_the_review_asks_the_steps_that_need_no_estimate_and_stops_at_the_first_that_does(monkeypatch):
    sp = spec(max_vol_rel=1.6, require_spot_center_agree=True, max_same_side=2, entry_spike_seconds=90)
    monkeypatch.setattr(volregime, "regime", volregime.VolRegime())
    volregime.regime.set(variants.coin_of("KXGOLD15M"), 1.0, 1.0)
    monkeypatch.setattr(runner, "side_spike", lambda *a, **k: (0.0, 0.0))
    trail = buychain.walk(buychain.Ctx(config.load(), sp, None), shot(), ("price", "vol_regime", "spike", "center_agree", "same_side"))
    assert trail.alive and [n for n, _ in trail.said] == ["price", "vol_regime", "spike"]  # the review has no estimate: center_agree and same_side wait for the decision


LANES_FILE = Path(__file__).resolve().parents[1] / "data" / "strategies.toml"
LIVE = ("commodity_energy", "commodity_wti", "commodity_natgas", "commodity_precious", "commodity_industrial")
SELLING = ("shock_", "wrong_side", "burst_", "cash_", "scalp_", "trail_", "stop_", "value_exit", "strike_through", "drop_", "bounce_", "take_profit", "late_drop", "settlement_", "watch_at", "watch_loss", "watch_ride", "watch_patient", "unstable_", "hover_", "spike_", "rest_sell")


def test_unit_no_lane_setting_sells_outside_an_exit_module():
    names = {f.name for f in dataclasses.fields(lanes.LaneSpec)}
    assert not [n for n in names if n.startswith(SELLING)]
    assert {n for n in names if n.startswith("curve_")} <= {n for n in names if lanes.exit_part(n) is not None}  # every curve_ setting belongs to an exit module's profit or loss part


@pytest.mark.skipif(not LANES_FILE.exists(), reason="no local lane file")
def test_integration_the_live_lanes_buy_and_sell_only_through_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    got = lanes.lane_specs_from(config.load(), LANES_FILE.read_text())
    for name in LIVE:
        sp = got[name]
        assert sp.buy_module and sp.buy_chain and sp.exit_module and sp.sell_chain, name
        assert sp.buy_chain[:1] == ("price",) and "perp_worth" in sp.buy_chain and "cushion" in sp.buy_chain, name
        assert "center_agree" in sp.buy_chain, name  # 2026-11-21 (user): all five live commodity lanes, the energy ones included, ask that the spot and its corrected center agree (the base lane buys by commodity_live_spot_center). Before this the energy lanes left the step out
    assert got["commodity_wti"].min_cushion_z == 0.8 and got["commodity_wti"].max_vol_rel == 1.6 and got["commodity_wti"].curve_take_trail == 1.0


def test_integration_a_lane_with_no_exit_module_is_not_watched_and_so_sells_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.bare]\nestimator='ladder'\nprice=[0.5,0.9]\nhours=[0.0,0.26]\ntimed_suffix='15M'\nseries=['KXBTC15M']\ncurve_take=true\n")
    s = config.load()
    store = Store(s.db_path)
    store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)", (time.time() - 100, "KXBTC15M-30JAN010015-15", "bet", "trade", 0.8, "yes", "bare", 4, 3, "2099-01-01T00:15:00Z"))
    store.db.commit()
    w = watcher.ProfitWatcher(lambda: s, object(), lambda: Store(s.db_path))
    w.refresh()
    assert w.targets == {}


def test_unit_an_exit_module_needs_steps_and_a_lane_may_not_write_its_own_chain_beside_one(tmp_path, monkeypatch):
    from tests.test_buychain import BASE, load

    with pytest.raises(ValueError, match="needs `steps`"):
        load(tmp_path, monkeypatch, f"[exit.e]\n[exit.e.profit]\n[exit.e.loss]\n\n[lane.a]\n{BASE}exit_module='e'\n")
    with pytest.raises(ValueError, match="writes sell_chain too"):
        load(tmp_path, monkeypatch, f"[exit.e]\nsteps=['under_even']\n[exit.e.profit]\n[exit.e.loss]\n\n[lane.a]\n{BASE}exit_module='e'\nsell_chain=['trail']\n")
    _, got = load(tmp_path, monkeypatch, f"[exit.e]\nsteps=['under_even']\ncurve_risk_live=true\n[exit.e.profit]\ncurve_take=true\n[exit.e.loss]\ncurve_risk_under_even=0.1\n\n[lane.a]\n{BASE}exit_module='e'\n")
    assert got["a"].sell_chain == ("under_even",) and got["a"].curve_take and got["a"].curve_risk_under_even == 0.1


def test_integration_a_step_that_says_no_keeps_the_buy_out_and_the_note_names_it(tmp_path, monkeypatch):
    from tests.test_buychain import LANE, Fixed

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(chain="['price','spike']").replace("buy_chain=", "entry_spike_seconds=90\nbuy_chain="))
    s = config.load()
    store = Store(s.db_path)
    sh = LongShot("KXBTC15M-7", "t", "", 0.79, 0.80, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="ch", series="KXBTC15M", side="yes", event_ticker="KXBTC15M-7")
    monkeypatch.setattr(runner, "side_spike", lambda *a, **k: (0.40, 0.10))
    out = runner.evaluate(s, None, None, store, sh, estimators={"market": Fixed(0.9)})
    assert out.action == "pass" and "spike NO" in out.note
    monkeypatch.setattr(runner, "side_spike", lambda *a, **k: (0.02, 0.05))
    out = runner.evaluate(s, None, None, store, sh, estimators={"market": Fixed(0.9)})
    assert out.action == "bet" and "spike yes" in out.note


# ---- the time_to_go sell step: time based, a one-moment dip does not sell (2026-11-19, user) ----
RISK = curverisk.Risk(0.3, 0.3, 0.3, "price")


def _ttg(tmp_path, monkeypatch):
    from tests.test_risk_exit_flow import held

    return held(tmp_path, monkeypatch, live=True, extra="sell_chain=['time_to_go']\ncurve_risk_time_to_go=240\ncurve_risk_time_to_go_wait_share=0.25")


def _read(monkeypatch, p):
    from kalshi_moonshot import curvevalue

    class R:
        pass

    r = R()
    r.p, r.cushion, r.rate_long = p, -0.001, 0.0001
    monkeypatch.setattr(curvevalue, "read", lambda *a, **k: r)
    monkeypatch.setattr(variants, "comeback_stats", lambda *a, **k: None)
    monkeypatch.setattr(ticks.history, "window", lambda *a, **k: ([0.0], [1.0]))


def test_unit_time_to_go_waits_a_share_of_the_time_left_and_a_dip_never_shortens_it(tmp_path, monkeypatch):
    from tests.test_risk_exit_flow import CLOSE, TICKER

    w = _ttg(tmp_path, monkeypatch)
    t = w.targets[(TICKER, "risk_only")]
    t.shock_ref, t.shock_index = 100.0, "IDX"
    _read(monkeypatch, 0.1)  # deep under the target
    now = CLOSE - 200
    first = w._sell_time_to_go(t, RISK, 0.30, now, 200.0, -0.5, None)
    assert first is None  # not at the first look: a one-moment dip
    assert w._sell_time_to_go(t, RISK, 0.30, now + 30, 170.0, -0.5, None) is None  # 30 s of a 50 s wait (a quarter of 200 s)
    got = w._sell_time_to_go(t, RISK, 0.30, now + 51, 149.0, -0.5, None)
    assert got and got.startswith("curve risk exit (time to go)")


def test_unit_time_to_go_restarts_when_the_chance_comes_back_and_does_not_act_outside_its_window(tmp_path, monkeypatch):
    from tests.test_risk_exit_flow import CLOSE, TICKER

    w = _ttg(tmp_path, monkeypatch)
    t = w.targets[(TICKER, "risk_only")]
    t.shock_ref, t.shock_index = 100.0, "IDX"
    now = CLOSE - 200
    _read(monkeypatch, 0.1)
    assert w._sell_time_to_go(t, RISK, 0.30, now, 200.0, -0.5, None) is None
    _read(monkeypatch, 0.8)  # the market recovered
    assert w._sell_time_to_go(t, RISK, 0.60, now + 40, 160.0, -0.5, None) is None and t.risk_go_since == 0.0
    _read(monkeypatch, 0.1)
    assert w._sell_time_to_go(t, RISK, 0.30, now + 60, 140.0, -0.5, None) is None  # the wait starts again
    assert w._sell_time_to_go(t, RISK, 0.30, now + 80, 120.0, -0.5, None) is None
    assert w._sell_time_to_go(t, RISK, 0.30, CLOSE - 400, 400.0, -0.5, None) is None  # outside the last 240 s


def test_unit_cooldown_blocks_a_market_the_lane_just_sold_on_either_side(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    store = Store(config.load().db_path)
    now = time.time()
    store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time, exit_ts, exit_price) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (now - 200, TICKER, "bet", "observe", 0.8, "yes", "x", 1, 1, "2099-01-01T00:00:00Z", now - 30, 0.9))
    store.db.commit()
    sp = spec(reentry_cooldown_seconds=90)
    for side in ("yes", "no"):
        got = ask("cooldown", sp, shot(side), p_yes=0.9, store=store)
        assert got.ok is False and "30s ago" in got.why
    assert ask("cooldown", spec(reentry_cooldown_seconds=20), p_yes=0.9, store=store).ok is True  # past its wait
    assert ask("cooldown", spec(), p_yes=0.9, store=store).ok is False  # a lane that sets no wait: the step is a no, as every step of a lane that sets nothing for it


# --- 2026-11-21: the cushion and variety steps read Kalshi's own price, never the outside feed, and memory only ---

def _fill(index, now, open_value, value, wobble=0.0005):
    """Ticks every second from 20 minutes back: `open_value` until the market opened (500 s ago), `value` since, with a small wobble so there is a rate."""
    for k in range(1201):
        t = now - 1200 + k
        ticks.history.note(index, t, (open_value if t < now - 500 else value) * (1.0 + wobble * (k % 2)))


def test_unit_a_commodity_snapshot_reads_the_kalshi_perp_and_not_the_outside_feed(monkeypatch):
    from kalshi_moonshot import biquotefeed, perpref

    ticks.history.clear()
    monkeypatch.setattr(variants.db, "reader", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the tape must not be read in a buy decision")))
    now, s = 50_000.0, config.load()
    try:
        _fill(perpref.index_for_series("KXGOLD15M"), now, 100.0, 101.0)  # Kalshi's perp: up 1% since the open, YES is ahead
        _fill(biquotefeed.index_for_series("KXGOLD15M"), now, 100.0, 99.0)  # the outside feed says the opposite
        assert variants.kalshi_index("KXGOLD15M", now) == perpref.index_for_series("KXGOLD15M")
        yes, no = variants.snapshot_now(s, "KXGOLD15M", "yes", 400.0, now=now), variants.snapshot_now(s, "KXGOLD15M", "no", 400.0, now=now)
        assert yes["z"] > 0 > no["z"]  # the perp's side, not the outside feed's
    finally:
        ticks.history.clear()


def test_unit_a_silent_perp_is_no_reading_and_the_outside_feed_is_not_a_fallback(monkeypatch):
    from kalshi_moonshot import biquotefeed, perpref

    ticks.history.clear()
    monkeypatch.setattr(variants.db, "reader", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the tape must not be read in a buy decision")))
    now, s = 50_000.0, config.load()
    try:
        _fill(biquotefeed.index_for_series("KXGOLD15M"), now, 100.0, 101.0)  # only the outside feed ticks
        assert variants.kalshi_index("KXGOLD15M", now) is None and variants.snapshot_now(s, "KXGOLD15M", "yes", 400.0, now=now) is None
        _fill(perpref.index_for_series("KXGOLD15M"), now - 60.0, 100.0, 101.0)  # a perp whose newest tick is a minute old is not a live price
        assert variants.kalshi_index("KXGOLD15M", now) is None and variants.snapshot_now(s, "KXGOLD15M", "yes", 400.0, now=now) is None
    finally:
        ticks.history.clear()


def test_integration_the_cushion_step_follows_the_perp_through_the_real_reading(monkeypatch):
    """No stub of the reading: ticks in memory, the step, the answer. The perp says YES is ahead by a wide cushion and the outside feed says NO is; the step passes YES and refuses NO. With the perp silent the cushion step is a no."""
    from kalshi_moonshot import biquotefeed, perpref

    ticks.history.clear()
    monkeypatch.setattr(variants.db, "reader", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the tape must not be read in a buy decision")))
    sp, now = spec(min_cushion_z=0.8), 50_000.0
    try:
        _fill(perpref.index_for_series("KXGOLD15M"), now, 100.0, 101.0)
        _fill(biquotefeed.index_for_series("KXGOLD15M"), now, 100.0, 99.0)
        yes, no = ask("cushion", sp, shot("yes"), now=now), ask("cushion", sp, shot("no"), now=now)
        assert yes.ok is True and "cushion z +" in yes.why and no.ok is False and "under the lane's" in no.why
        assert "variety " in yes.why
        ticks.history.clear()
        _fill(biquotefeed.index_for_series("KXGOLD15M"), now, 100.0, 101.0)
        assert ask("cushion", sp, shot("yes"), now=now).ok is False and "cannot be read" in ask("cushion", sp, shot("yes"), now=now).why
    finally:
        ticks.history.clear()


# --- 2026-11-21: the context_score step is removed; a lane file that still names it loads without it ---

def test_unit_the_context_score_step_and_its_module_are_gone():
    import importlib

    assert "context_score" not in lanes.BUY_STEPS and "context_score" not in buychain.STEPS and "context_score" in lanes.RETIRED_STEPS
    with pytest.raises(ImportError):
        importlib.import_module("kalshi_moonshot.contextscore")


def test_integration_a_lane_file_that_still_names_context_score_loads_without_it(tmp_path, monkeypatch, caplog):
    """Lane file -> module -> spec: a config written for the older image does not stop the bot; the retired step is dropped and logged, the other steps keep their order, and an unknown step is still refused."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    text = "[module.m]\nsteps=['price','cushion','context_score','cooldown']\nmin_cushion_z=0.8\n\n[lane.a]\nestimator='market'\nprice=[0.60,0.92]\nhours=[0.0,1.0]\nseries=['KXGOLD15M']\nmax_spread=0.05\nmin_volume=0\nbuy_module='m'\n"
    with caplog.at_level("WARNING", logger="moonshot"):
        sp = lanes.lane_specs_from(config.load(), text)["a"]
    assert sp.buy_chain == ("price", "cushion", "cooldown") and "context_score" in caplog.text
    with pytest.raises(ValueError):
        lanes.lane_specs_from(config.load(), text.replace("context_score", "no_such_step"))


@pytest.mark.skipif(not LANES_FILE.exists(), reason="no local lane file")
def test_integration_the_real_lane_file_loads_and_no_lane_keeps_the_retired_step(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    got = lanes.lane_specs_from(config.load(), LANES_FILE.read_text())
    assert got and all("context_score" not in sp.buy_chain for sp in got.values())


def test_unit_side_losing_refuses_more_of_a_side_whose_open_bets_stand_under_water(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    store = Store(config.load().db_path)
    for tk in ("KXBTC15M-30JAN010015-15", "KXETH15M-30JAN010015-15"):
        store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)", (time.time(), tk, "bet", "observe", 0.8, "yes", "x", 10, 8.0, "2099-01-01T00:00:00Z"))
    store.db.commit()
    sp = spec()
    new = dataclasses.replace(shot(), ticker="KXSOL15M-30JAN010015-15", series="KXSOL15M")
    monkeypatch.setattr(ticks.mids, "quotes", lambda t: [])
    assert ask("side_losing", sp, new, store=store).ok is True  # no quote in memory yet: nothing to count
    monkeypatch.setattr(ticks.mids, "quotes", lambda t: [(time.time(), 0.55, 0.57)])
    assert ask("side_losing", sp, new, store=store).ok is False  # paid 0.80, the bid is 0.55
    sp20 = spec(side_losing_share=0.2)
    assert ask("side_losing", sp20, new, store=store).ok is False  # about 33% under what they cost
    monkeypatch.setattr(ticks.mids, "quotes", lambda t: [(time.time(), 0.70, 0.72)])
    assert ask("side_losing", sp20, new, store=store).ok is True  # about 15% under: not enough
    assert ask("side_losing", sp, new, store=store).ok is False  # with no share any loss refuses
    monkeypatch.setattr(ticks.mids, "quotes", lambda t: [(time.time(), 0.90, 0.92)])
    assert ask("side_losing", sp, new, store=store).ok is True  # ahead at the bid
    monkeypatch.setattr(ticks.mids, "quotes", lambda t: [(time.time(), 0.55, 0.57)])
    assert ask("side_losing", sp, shot("no", 0.2), store=store).ok is True  # the open bets are YES: the NO side is another side
    assert ask("side_losing", sp, new).ok is True  # the review has no books


def test_unit_side_losing_blocks_when_any_one_open_bet_is_under_the_share_whatever_the_total(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    store = Store(config.load().db_path)
    for tk in ("KXBTC15M-30JAN010015-15", "KXETH15M-30JAN010015-15"):
        store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)", (time.time(), tk, "bet", "observe", 0.8, "yes", "x", 10, 8.0, "2099-01-01T00:00:00Z"))
    store.db.commit()
    new = dataclasses.replace(shot(), ticker="KXSOL15M-30JAN010015-15", series="KXSOL15M")
    bids = {"KXBTC15M-30JAN010015-15": (0.70, 0.72), "KXETH15M-30JAN010015-15": (0.98, 0.99)}  # BTC about 15% under what it cost, ETH well ahead: together they are in profit
    monkeypatch.setattr(ticks.mids, "quotes", lambda t: [(time.time(), *bids[t])])
    sp = spec(side_losing_share=0.1)
    c = ask("side_losing", sp, new, store=store)
    assert c.ok is False and "KXBTC15M" in c.why  # 2026-11-25, user: "if ANY are losing by side_losing_share it should block more buys", "they all share the same losses"
    assert ask("side_losing", spec(side_losing_share=0.2), new, store=store).ok is True


def test_unit_side_losing_values_an_open_bet_at_the_better_of_its_bid_and_its_hold_on_the_cushion(tmp_path, monkeypatch):
    from kalshi_moonshot import binaryrisk

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    store = Store(config.load().db_path)
    store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)", (time.time(), "KXBTC15M-30JAN010015-15", "bet", "observe", 0.8, "yes", "x", 10, 8.0, "2099-01-01T00:00:00Z"))
    store.db.commit()
    new = dataclasses.replace(shot(), ticker="KXSOL15M-30JAN010015-15", series="KXSOL15M")
    now = time.time()
    monkeypatch.setattr(ticks.mids, "quotes", lambda t: [(now, 0.55, 0.57)])  # the bid has dipped from the 0.80 paid: 33% under at the bid
    sp = spec(side_losing_share=0.05)
    monkeypatch.setattr(binaryrisk, "curve_index", lambda series: "IDX")
    T = [now - 620.0 + i for i in range(621)]
    V = [100.0 + (0.01 if i % 2 else -0.01) for i in range(621)]  # a spot of about 100 moving a hundredth a second
    monkeypatch.setattr(ticks.history, "window", lambda index, a, b: (T, V))
    monkeypatch.setattr(ticks.strikes, "get", lambda tk, *a: 99.0)  # a cushion of 1.0, about a hundred typical minutes
    c = ask("side_losing", sp, new, store=store, now=now)
    assert c.ok is True and "hold value" in c.why  # 2026-11-25, user: "side_losing needs to account for cushion also": a dip with plenty of cushion is not losing
    monkeypatch.setattr(ticks.strikes, "get", lambda tk, *a: 100.0)  # the spot sits on the target: no cushion
    assert ask("side_losing", sp, new, store=store, now=now).ok is False  # the bid decides again
    monkeypatch.setattr(ticks.strikes, "get", lambda tk, *a: None)  # no target in memory
    assert ask("side_losing", sp, new, store=store, now=now).ok is False


def test_unit_side_losing_counts_the_move_since_the_buy_not_the_spread_and_fees_paid_at_it(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    store = Store(config.load().db_path)
    store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, bid, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (time.time(), "KXBTC15M-30JAN010015-15", "bet", "observe", 0.77, 0.79, "yes", "x", 10, 7.9, "2099-01-01T00:00:00Z"))  # bought at 0.79 with the bid at 0.77: a 2c spread
    store.db.commit()
    new = dataclasses.replace(shot(), ticker="KXSOL15M-30JAN010015-15", series="KXSOL15M")
    sp = spec(side_losing_share=0.05)
    now = time.time()
    monkeypatch.setattr(ticks.mids, "quotes", lambda t: [(now, 0.77, 0.79)])  # nothing has moved since the buy
    c = ask("side_losing", sp, new, store=store, now=now)
    assert c.ok is True and "0% under where it stood when bought" in c.why  # the spread and fees alone (about 6% under its cost at the bid) no longer block (2026-11-25, user: "i think we need to fix that gap")
    monkeypatch.setattr(ticks.mids, "quotes", lambda t: [(now, 0.74, 0.76)])  # the bid has fallen 3c since the buy: about 4% of its cost
    assert ask("side_losing", sp, new, store=store, now=now).ok is True
    monkeypatch.setattr(ticks.mids, "quotes", lambda t: [(now, 0.70, 0.72)])  # 7c down: about 9% under
    assert ask("side_losing", sp, new, store=store, now=now).ok is False


def test_unit_settle_confirm_buys_only_once_the_spot_has_moved_the_bets_way_by_its_own_typical_minute(monkeypatch):
    from kalshi_moonshot import binaryrisk, priskp

    monkeypatch.setattr(buychain, "_CONFIRM_FIRST", {})
    spot = {"v": 100.0}
    clock = {"t": 1000.0}
    monkeypatch.setattr(binaryrisk, "curve_index", lambda series: "IDX")
    monkeypatch.setattr(ticks.history, "last_at_or_before", lambda index, ts: (clock["t"], spot["v"]))
    monkeypatch.setattr(ticks.history, "window", lambda index, a, b: ([0.0, 1.0], [100.0, 100.0]))
    monkeypatch.setattr(priskp, "typical_minute", lambda T, V, now, span=900.0: 0.5)
    sp = spec(settle_confirm_units=1.0)  # needs a move of 0.5

    def live(sh=None):
        return ask("settle_confirm", sp, sh, now=clock["t"])

    assert live().ok is False  # the wait begins at 100.0
    spot["v"] = 100.3
    clock["t"] += 5.0
    assert live().ok is False  # moved 0.3 of 0.5: no direction yet
    spot["v"] = 99.5
    clock["t"] += 5.0
    assert live().ok is False  # moved against a YES bet
    spot["v"] = 100.6
    clock["t"] += 5.0
    ok = live()
    assert ok.ok is True and "confirmed" in ok.why  # 0.6 with the bet, no clock involved
    assert live(shot("no", 0.2)).ok is False  # a NO's own wait has only just begun
    spot["v"] = 99.4
    clock["t"] += 3.0
    assert live(shot("no", 0.2)).ok is True  # a fall of 0.6 is with a NO bet
    assert ask("settle_confirm", spec(), now=clock["t"]).ok is False  # no setting on the lane: a no


def test_unit_the_heavy_steps_are_throttled_and_the_ticks_between_read_the_kept_answer(monkeypatch):
    """User, 2026-11-25: "the only thing we need evaluated every tick, is the market pricing. others can be throttled"."""
    calls = {"n": 0}

    def fake(ctx, shot):
        calls["n"] += 1
        return buychain.Check(True, f"call {calls['n']}")

    wrapped = buychain.throttled("probe")(fake)
    monkeypatch.setattr(buychain, "THROTTLE_S", 3.0)
    buychain._THROTTLED.clear()
    sp = spec()
    c1 = wrapped(buychain.Ctx(config.load(), sp, now=1000.0), shot())
    c2 = wrapped(buychain.Ctx(config.load(), sp, now=1002.0), shot())  # 2 s later: the kept answer
    assert c1.why == c2.why == "call 1" and calls["n"] == 1
    c3 = wrapped(buychain.Ctx(config.load(), sp, now=1003.5), shot())  # past the throttle: asked again
    assert c3.why == "call 2" and calls["n"] == 2
    c4 = wrapped(buychain.Ctx(config.load(), sp, now=1003.6), shot("no"))  # another side has its own answer
    assert c4.why == "call 3"
    monkeypatch.setattr(buychain, "THROTTLE_S", 0.0)
    assert wrapped(buychain.Ctx(config.load(), sp, now=1003.7), shot()).why == "call 4"  # off: every tick
