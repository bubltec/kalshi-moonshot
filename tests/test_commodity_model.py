"""The commodity buy model as a buy module (2026-11-18, user, on the commodity lanes' min_cushion_z, max_vol_rel, buy_curve_edge_min: "these are model concepts, they shouldn't be lane options"; "they are also antiquated, lets revise them in a new model for commodities"): the lane's gates say what is eligible (price), the risk score picks the side, then two checks on Kalshi's own perp reference confirm: perp_worth (its chance for the side is within the lane's allowance of the ask) and calm (the market is not in the anomaly state of CLAUDE.md rule 3, in place of a fixed volatility cap). Unit tests on the two steps over real ticks in memory; integration through a lane file with the module into runner.evaluate."""
import math
import random
import time

import pytest

from kalshi_moonshot import (
    buychain,
    config,
    curveexit,
    lanes,
    regime,
    riskscore,
    runner,
    ticks,
    variants,
)
from kalshi_moonshot.estimators import Estimate
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot

STEPS = "['price','risk_score','perp_worth','calm']"
FILE = (
    "[module.commodity_model]\nlabel='gates, then the risk score picks the side, then the Kalshi perp confirms'\nsteps={steps}\nrisk_score_buy=0.25\nbuy_curve_edge_min={floor}\nspot_source='kalshi'\n\n"
    "[lane.cm]\nestimator='market'\nprice=[0.60,0.92]\nhours=[0.0,1.0]\nsides=['yes','no']\nfixed_stake=5.0\nseries=['KXSILVER15M']\nmax_spread=0.05\nmin_volume=0\nbuy_module='commodity_model'\n"
)
PERP = "KP_SILVER"
LEFT = 400.0  # seconds to the close: the window opened 500 s ago
NOISE = 2e-5  # the one-second move of the test's perp, as a log step


@pytest.fixture(autouse=True)
def clean():
    yield
    ticks.mids.clear()
    ticks.history.clear()
    buychain.DROPPED.clear()
    curveexit._cuts.clear()
    curveexit._cuts_at.clear()


def perp(now, drift_bp=0.0, wild=1.0, seconds=11160, index=PERP):
    """Three hours of one-second perp ticks ending at `now`: a random walk, moved `drift_bp` over the 150 s after the window opened (up = YES ahead of its opening price), its last 15 minutes `wild` times as jumpy as the rest."""
    rng = random.Random(7)
    opened = now + LEFT - 900.0
    v = 50.0
    for k in range(seconds + 1):
        t = now - seconds + k
        step = rng.gauss(0.0, NOISE * (wild if t > now - 900.0 else 1.0))
        if opened < t <= opened + 150.0:
            step += drift_bp * 1e-4 / 150.0
        v *= math.exp(step)
        ticks.history.note(index, t, v)


def settings(tmp_path, monkeypatch, steps=STEPS, floor=-0.05, yes_score=0.80):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(FILE.format(steps=steps, floor=floor))
    monkeypatch.setattr(riskscore, "live", lambda series, ticker, side, *a, **k: ([yes_score if side == "yes" else 1.0 - yes_score] * 3, 0.5, [0.5] * 3))
    return config.load()


def shot(side="yes", bid=0.79, ask=0.80):
    return LongShot("KXSILVER15M-7", "t", "", bid, ask, LEFT / 3600.0, "2099-01-01T00:00:00Z", LEFT / 3600.0, lane="cm", series="KXSILVER15M", side=side, event_ticker="KXSILVER15M-7")


def ctx(s, now):
    return buychain.Ctx(s, lanes.spec_for(s, "cm"), None, now)


# perp_worth


def test_unit_perp_worth_passes_a_side_the_perp_has_ahead_and_refuses_the_other(tmp_path, monkeypatch):
    s, now = settings(tmp_path, monkeypatch), time.time()
    perp(now, drift_bp=10.0)  # the perp is 10 bp over its price at the open: YES is well ahead
    yes = buychain.perp_worth_step(ctx(s, now), shot("yes"))
    no = buychain.perp_worth_step(ctx(s, now), shot("no", bid=0.19, ask=0.21))
    assert yes.ok and "the perp's chance 0.9" in yes.why
    assert not no.ok and "under the ask 0.21" in no.why and "the lane allows 0.05" in no.why


def test_unit_perp_worth_reads_the_same_number_as_the_lane_option_it_replaces(tmp_path, monkeypatch):
    s, now = settings(tmp_path, monkeypatch), time.time()
    perp(now, drift_bp=1.0)
    p, e = variants.curve_edge_now("KXSILVER15M", "yes", LEFT, 0.80, None, now=now, source="kalshi")
    got = buychain.perp_worth_step(ctx(s, now), shot("yes"))
    assert got.ok == (e >= -0.05) and f"{p:.3f}" in got.why


def test_unit_perp_worth_the_allowance_is_the_lanes(tmp_path, monkeypatch):
    now = time.time()
    perp(now, drift_bp=1.0)
    p, _ = variants.curve_edge_now("KXSILVER15M", "yes", LEFT, 0.80, None, now=now, source="kalshi")
    assert 0.55 < p < 0.90  # a chance that an ask of p + 0.10 passes or fails by the allowance alone
    dear = shot("yes", bid=round(p + 0.09, 2), ask=round(p + 0.10, 2))
    assert not buychain.perp_worth_step(ctx(settings(tmp_path, monkeypatch, floor=-0.05), now), dear).ok
    assert buychain.perp_worth_step(ctx(settings(tmp_path, monkeypatch, floor=-0.15), now), dear).ok


def test_unit_perp_worth_with_no_reading_is_a_no_and_says_why(tmp_path, monkeypatch):
    s, now = settings(tmp_path, monkeypatch), time.time()
    silent = buychain.perp_worth_step(ctx(s, now), shot())  # no ticks at all
    assert not silent.ok and "silent" in silent.why
    monkeypatch.setattr(variants, "biquote_index", lambda series: "BQ_SILVER")
    perp(now, drift_bp=10.0, index="BQ_SILVER")  # only the outside feed has a price: the step does not read it
    outside = buychain.perp_worth_step(ctx(s, now), shot())
    assert not outside.ok and "silent" in outside.why
    ticks.history.clear()
    perp(now, drift_bp=10.0, seconds=120)  # the perp is ticking but has two minutes of history
    short = buychain.perp_worth_step(ctx(s, now), shot())
    assert not short.ok and "cannot be read" in short.why


def test_unit_perp_worth_on_a_lane_with_no_allowance_is_a_no(tmp_path, monkeypatch):
    s, now = settings(tmp_path, monkeypatch, floor=-9.0), time.time()
    perp(now, drift_bp=10.0)
    got = buychain.perp_worth_step(ctx(s, now), shot())
    assert not got.ok and "buy_curve_edge_min" in got.why


# calm


def test_unit_calm_passes_a_market_inside_its_own_range(tmp_path, monkeypatch):
    s, now = settings(tmp_path, monkeypatch), time.time()
    perp(now)
    got = buychain.calm_step(ctx(s, now), shot())
    assert got.ok and "anomaly" not in got.why and "x its median" in got.why


def test_unit_calm_refuses_a_market_whose_typical_minute_is_over_three_times_its_median(tmp_path, monkeypatch):
    s, now = settings(tmp_path, monkeypatch), time.time()
    perp(now, wild=5.0)  # the last 15 minutes are five times as jumpy as the three hours before
    got = buychain.calm_step(ctx(s, now), shot())
    assert not got.ok and got.why.startswith("anomaly") and "sits out" in got.why
    state, r, cuts = curveexit.state_now(PERP, now)
    assert state == "anomaly" and r["unit"] > 3.0 * cuts["unit_med"]


def test_unit_calm_the_cutoff_is_the_markets_own_not_a_fixed_number(tmp_path, monkeypatch):
    """The same last 15 minutes are an anomaly on a quiet market and normal on one that has been that jumpy for three hours."""
    s, now = settings(tmp_path, monkeypatch), time.time()
    perp(now, wild=5.0)
    assert not buychain.calm_step(ctx(s, now), shot()).ok
    ticks.history.clear()
    curveexit._cuts.clear()
    curveexit._cuts_at.clear()
    monkeypatch.setattr("tests.test_commodity_model.NOISE", 5 * NOISE)
    perp(now)
    assert buychain.calm_step(ctx(s, now), shot()).ok


def test_unit_calm_with_no_state_to_read_is_a_no(tmp_path, monkeypatch):
    s, now = settings(tmp_path, monkeypatch), time.time()
    silent = buychain.calm_step(ctx(s, now), shot())
    assert not silent.ok and "silent" in silent.why
    perp(now, seconds=1200)  # 20 minutes of ticks: too few readings for the market's own cutoffs
    young = buychain.calm_step(ctx(s, now), shot())
    assert not young.ok and "cannot be read yet" in young.why


def test_unit_the_new_steps_need_no_estimate_so_the_review_asks_them(tmp_path, monkeypatch):
    s, now = settings(tmp_path, monkeypatch), time.time()
    perp(now, drift_bp=-10.0)  # the perp has YES behind
    assert not {"perp_worth", "calm"} & buychain.NEEDS_ESTIMATE
    kept = buychain.screen(ctx(s, now), [shot("yes")])
    assert kept == [] and "out at perp_worth" in buychain.DROPPED["cm"][0]["reason"]


# through a lane file into the decision


class Fixed:
    def estimate(self, client, settings, shot):
        return Estimate(shot.ask, "the price asked")


def decide(tmp_path, monkeypatch, drift_bp=10.0, wild=1.0, yes_score=0.80, steps=STEPS):
    s = settings(tmp_path, monkeypatch, steps=steps, yes_score=yes_score)
    store = Store(s.db_path)
    regime.reset()
    now = time.time()
    perp(now, drift_bp=drift_bp, wild=wild)
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXSILVER15M-7", "KXSILVER15M", now + LEFT, 50.0, now))
    store.db.commit()
    return runner.evaluate(s, None, None, store, shot(), estimators={"market": Fixed()})


def test_integration_a_side_that_passes_every_step_is_bought_and_the_note_names_the_module(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch)  # price 0.80 in range, risk 20% and held, the perp has YES ahead, the market inside its own range
    assert out.action == "bet", out.note
    assert "[chain commodity_model: price > risk_score > perp_worth > calm |" in out.note and "YES passed every step" in out.note
    assert "[curve edge" not in out.note  # the lane option's own check in the decision is not asked a second time


def test_integration_the_perp_check_refuses_a_side_the_risk_score_picked(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, drift_bp=-10.0)  # the score favours YES; Kalshi's perp has it well behind its opening price
    assert out.action == "pass" and "YES out at perp_worth" in out.note and "under the ask 0.80" in out.note


def test_integration_an_anomaly_refuses_a_side_every_other_step_passed(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, drift_bp=0.0, wild=5.0, steps="['price','risk_score','calm']")
    assert out.action == "pass" and "YES out at calm" in out.note and "anomaly" in out.note


def test_integration_the_risk_score_still_picks_the_side_before_the_perp_is_asked(tmp_path, monkeypatch):
    out = decide(tmp_path, monkeypatch, yes_score=0.60)  # a risk of 40%: the side is out before the perp is read
    assert out.action == "pass" and "YES out at risk_score" in out.note and "perp_worth" not in out.note.split("|")[1]


def test_integration_a_lane_that_writes_the_option_and_no_chain_is_gated_as_before(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[lane.cm]\nestimator='market'\nalways=true\nprice=[0.60,0.92]\nhours=[0.0,1.0]\nsides=['yes','no']\nfixed_stake=5.0\nseries=['KXSILVER15M']\nmax_spread=0.05\nmin_volume=0\nspot_source='kalshi'\nbuy_curve_edge_min=-0.05\n")
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    now = time.time()
    perp(now, drift_bp=-10.0)
    out = runner.evaluate(s, None, None, store, shot(), estimators={"market": Fixed()})
    assert out.action == "pass" and "[curve edge" in out.note
