"""The buy rules the live commodity lanes had as lane settings, now steps of a buy module (2026-11-19, user: "we don't use anything outside modules for buying and selling").

Unit: each step answers yes or no on a stubbed reading. Integration: the real lane file's live lanes load with their module's steps and carry no buy or sell setting outside the modules; a lane with no exit module sells nothing."""
import dataclasses
import time
from pathlib import Path

import pytest

from kalshi_moonshot import buychain, config, lanes, runner, ticks, variants, volregime, watcher
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
    assert {"variety", "vol_regime", "cushion", "spike", "center_agree", "same_side", "price_trend", "state_record", "curve_model"} <= set(buychain.STEPS)


def test_unit_variety_skips_what_the_lane_names_and_does_not_skip_what_it_cannot_read(monkeypatch):
    sp = spec(skip_varieties=("G-no", "A-yes"))
    monkeypatch.setattr(variants, "variety_now", lambda *a, **k: "G")
    assert ask("variety", sp, shot("no")).ok is False and ask("variety", sp, shot("yes")).ok is True
    monkeypatch.setattr(variants, "variety_now", lambda *a, **k: None)
    got = ask("variety", sp, shot("no"))
    assert got.ok is True and "not skipped" in got.why
    assert ask("variety", spec()).ok is False  # no varieties named: the step is a no, as every step of a lane that sets nothing for it


def test_unit_vol_regime_wants_a_known_regime_under_the_ceiling(monkeypatch):
    sp = spec(max_vol_rel=1.6)
    coin = variants.coin_of("KXGOLD15M")
    monkeypatch.setattr(volregime, "regime", volregime.VolRegime())
    assert "unknown" in ask("vol_regime", sp).why and ask("vol_regime", sp).ok is False
    volregime.regime.set(coin, 1.2, 1.0)
    assert ask("vol_regime", sp).ok is True
    volregime.regime.set(coin, 1.7, 1.0)
    assert ask("vol_regime", sp).ok is False and ask("vol_regime", spec()).ok is False


def test_unit_cushion_wants_a_cushion_and_few_crossings(monkeypatch):
    sp = spec(min_cushion_z=0.8, max_spot_crosses=4)
    monkeypatch.setattr(variants, "snapshot_now", lambda *a, **k: {"z": 1.0, "crosses": 2})
    assert ask("cushion", sp).ok is True
    monkeypatch.setattr(variants, "snapshot_now", lambda *a, **k: {"z": 0.5, "crosses": 2})
    assert ask("cushion", sp).ok is False
    monkeypatch.setattr(variants, "snapshot_now", lambda *a, **k: {"z": 1.0, "crosses": 4})
    assert ask("cushion", sp).ok is False
    monkeypatch.setattr(variants, "snapshot_now", lambda *a, **k: None)
    assert ask("cushion", sp).ok is False and ask("cushion", spec()).ok is False


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
SELLING = ("shock_", "wrong_side", "burst_", "cash_", "scalp_", "trail_", "stop_", "value_exit", "strike_through", "drop_", "bounce_", "take_profit", "late_drop", "settlement_", "watch_at", "watch_loss", "watch_ride", "watch_patient", "unstable_", "hover_", "spike_", "rest_sell", "rebuy_")


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
    assert "center_agree" in got["commodity_precious"].buy_chain and "center_agree" not in got["commodity_wti"].buy_chain
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
    from tests.test_buychain import Fixed, LANE

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(LANE.format(chain="['price','spike']").replace("risk_score_buy=0.25\n", "entry_spike_seconds=90\n"))
    s = config.load()
    store = Store(s.db_path)
    sh = LongShot("KXBTC15M-7", "t", "", 0.79, 0.80, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane="ch", series="KXBTC15M", side="yes", event_ticker="KXBTC15M-7")
    monkeypatch.setattr(runner, "side_spike", lambda *a, **k: (0.40, 0.10))
    out = runner.evaluate(s, None, None, store, sh, estimators={"market": Fixed(0.9)})
    assert out.action == "pass" and "spike NO" in out.note
    monkeypatch.setattr(runner, "side_spike", lambda *a, **k: (0.02, 0.05))
    out = runner.evaluate(s, None, None, store, sh, estimators={"market": Fixed(0.9)})
    assert out.action == "bet" and "spike yes" in out.note
