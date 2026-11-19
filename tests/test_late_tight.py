"""The late_tight sell step: in the last share of the market a bet whose bid is K typical minutes under its high since the buy is sold, ahead or not (2026-11-19, user: "i dont think high volatility means we don't buy, but i think it means we tighten our selling"; "build the new step as a module"). Unit + integration: lane file -> watcher -> signals and rows."""
import json

import pytest

from kalshi_moonshot import config, lanes, watcher
from kalshi_moonshot import ticks as ticks_mod
from tests.test_risk_exit_flow import held, run_up, tick


@pytest.fixture(autouse=True)
def _clean():
    ticks_mod.mids._rows.clear()
    yield
    ticks_mod.mids._rows.clear()


def _spec(**kw):
    return lanes.LaneSpec("x", "ladder", "x", (0.70, 0.97), (0, 1), 0.05, 0.0, **kw)


def rows(tmp_path):
    f = tmp_path / "risk_shadow.jsonl"
    return [x for x in map(json.loads, f.read_text().splitlines()) if x.get("kind") == "late tight"] if f.exists() else []


def lane(tmp_path, monkeypatch, live="true", chain="['late_tight']", k="3.0", extra=""):
    return held(tmp_path, monkeypatch, live=True, take="true", extra=f"sell_chain={chain}\ncurve_risk_late_tight={k}\ncurve_risk_late_tight_live={live}\n{extra}")


def test_unit_the_fields_default_off_shadow_and_a_sixth_of_the_market_and_belong_to_the_loss_part_of_an_exit_module():
    spec = _spec()
    assert spec.curve_risk_late_tight == 0.0 and spec.curve_risk_late_tight_live is False and spec.curve_risk_late_tight_share == pytest.approx(1 / 6)
    assert lanes.exit_part("curve_risk_late_tight") == "loss" and lanes.exit_part("curve_risk_late_tight_live") == "loss"


def test_unit_only_a_live_late_tight_changes_the_lane_version(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = config.load()
    base = _spec()
    shadow = _spec(curve_risk_late_tight=3.0)
    live = _spec(curve_risk_late_tight=3.0, curve_risk_late_tight_live=True)
    assert lanes.settings_of(base, cfg) == lanes.settings_of(shadow, cfg) != lanes.settings_of(live, cfg)


def test_integration_live_it_sells_a_bet_that_fell_in_the_last_share_ahead_or_not_and_says_so(tmp_path, monkeypatch):
    w = lane(tmp_path, monkeypatch)
    run_up(w)  # bought 0.81, high 0.90 with 260 s left: outside the last 150 s
    assert tick(w, 0.90, 160) == []
    got = tick(w, 0.60, 140)  # 30c under the high, in the window, and under the buy price (behind: the trail would stand aside)
    assert got and "late tight" in got[0].why and "0.90 high since the buy" in got[0].why and "sold at late_tight" in got[0].why
    r = rows(tmp_path)
    assert r and r[0]["live"] is True and r[0]["high"] == 0.9 and r[0]["units"] == 3.0


def test_integration_shadow_it_logs_one_would_sell_and_sells_nothing(tmp_path, monkeypatch):
    w = lane(tmp_path, monkeypatch, live="false")
    run_up(w)
    tick(w, 0.90, 160)
    assert tick(w, 0.60, 140) == [] and tick(w, 0.58, 130) == [] and tick(w, 0.55, 120) == []
    r = rows(tmp_path)
    assert len(r) == 1 and r[0]["live"] is False and r[0]["bid"] == 0.6  # one row a bet, not one a check


def test_integration_outside_the_last_share_it_does_nothing(tmp_path, monkeypatch):
    w = lane(tmp_path, monkeypatch)
    run_up(w)
    assert tick(w, 0.60, 200) == [] and tick(w, 0.55, 180) == []  # more than 150 s left
    assert rows(tmp_path) == []


def test_integration_a_fall_inside_the_markets_own_swing_is_held(tmp_path, monkeypatch):
    w = lane(tmp_path, monkeypatch, k="3.0")
    run_up(w)
    tick(w, 0.90, 160)
    assert tick(w, 0.89, 140) == [] and rows(tmp_path) == []


def test_integration_a_lane_that_does_not_name_it_or_has_it_off_sells_nothing_on_it(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    w = lane(tmp_path / "a", monkeypatch, chain="['profit_take']")
    run_up(w)
    tick(w, 0.90, 160)
    assert tick(w, 0.60, 140) == [] and rows(tmp_path / "a") == []
    ticks_mod.mids._rows.clear()
    (tmp_path / "b").mkdir()
    w = lane(tmp_path / "b", monkeypatch, k="0")
    run_up(w)
    tick(w, 0.90, 160)
    assert tick(w, 0.60, 140) == [] and rows(tmp_path / "b") == []


def test_integration_a_sale_by_a_step_before_it_stands(tmp_path, monkeypatch):
    w = lane(tmp_path, monkeypatch, chain="['trail','late_tight']", extra="curve_take_trail=1.0\ncurve_take_trail_live=true")
    run_up(w)
    tick(w, 0.90, 160)
    got = tick(w, 0.84, 140)  # ahead of the buy and 6c under the high: the trail's sale
    assert got and "trail" in got[0].why and "late tight" not in got[0].why
    assert rows(tmp_path) == []


def test_unit_a_shadow_late_tight_in_the_chain_does_not_change_the_lane_version(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = config.load()
    plain = _spec(sell_chain=("trail", "long_hold"))
    shadow = _spec(sell_chain=("trail", "late_tight", "long_hold"), curve_risk_late_tight=3.0)
    live = _spec(sell_chain=("trail", "late_tight", "long_hold"), curve_risk_late_tight=3.0, curve_risk_late_tight_live=True)
    assert lanes.settings_of(plain, cfg) == lanes.settings_of(shadow, cfg) != lanes.settings_of(live, cfg)


def test_integration_there_is_no_bid_floor_a_bet_whose_bid_fell_under_five_cents_is_still_sold(tmp_path, monkeypatch):
    """2026-11-19, user: "we should remove loss AND the bid_floor concept". The 0.05 floor (CURVE_MIN_BID) that every sale step used to respect is gone: a bid of 0.03 is a bid like any other."""
    assert not hasattr(watcher, "CURVE_MIN_BID")
    w = lane(tmp_path, monkeypatch)
    run_up(w)
    tick(w, 0.90, 160)
    got = tick(w, 0.03, 140)
    assert got and "late tight" in got[0].why and "sells at 0.03" in got[0].why


def test_integration_under_even_also_sells_at_a_bid_under_five_cents(tmp_path, monkeypatch):
    from tests.test_risk_exit_flow import flat, quotes

    w = held(tmp_path, monkeypatch, live=True, extra="sell_chain=['under_even']\ncurve_risk_under_even=0.1\ncurve_risk_under_even_live=true")
    out = quotes(w, flat(0.78, 445, 305) + [(left, 0.03) for left in range(300, 200, -1)])
    assert out and out[0].why.startswith("curve risk exit (under even)") and "bid 0.03" in out[0].why
