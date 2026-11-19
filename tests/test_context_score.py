"""The context score: a shadow buy step that puts the logged readings' combined chance to win in a bet's note (2026-11-19, user: "yes", to a shadow step for the readings together). Unit + integration: fit, score, note, lane file -> bet row."""
import json
import math
import random

import pytest

from kalshi_moonshot import buychain, config, contextscore, lanes
from kalshi_moonshot.store import Store


@pytest.fixture(autouse=True)
def _clean():
    contextscore._cache.update({"mtime": None, "data": None})
    yield
    contextscore._cache.update({"mtime": None, "data": None})


def rows_with_signal(n=400, seed=3):
    """Bets whose win chance is the price's logit plus -2 x a dip reading and +1 x the book's imbalance: the readings carry something the price does not."""
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        ask = rnd.uniform(0.6, 0.92)
        dip, imb = rnd.uniform(0, 0.3), rnd.uniform(-1, 1)
        z = math.log(ask / (1 - ask)) - 2.0 * dip * 5 + 1.0 * imb
        won = 1 if rnd.random() < 1 / (1 + math.exp(-z)) else 0
        r = {"ask": ask, "left": rnd.uniform(60, 700), "dip_90": dip, "imbalance": imb, "imbalance_x_side": imb, "spread": 0.01, "elapsed_min": 8.0}
        out.append((r, won))
    return out


def test_unit_the_fit_learns_what_the_readings_add_to_the_price():
    m = contextscore.fit(rows_with_signal(), lam=10.0)
    assert m is not None and m["n"] == 400
    base = {"ask": 0.85, "left": 300.0, "dip_90": 0.02, "imbalance": 0.0, "imbalance_x_side": 0.0, "spread": 0.01, "elapsed_min": 8.0}
    dipped = dict(base, dip_90=0.25)
    leaning = dict(base, imbalance=0.9, imbalance_x_side=0.9)
    assert contextscore.predict(m, dipped) < contextscore.predict(m, base) < contextscore.predict(m, leaning)  # a deeper dip lowers it, a book leaning its way raises it


def test_unit_with_readings_that_say_nothing_the_score_stays_near_the_price_and_too_few_bets_fit_nothing():
    rnd = random.Random(5)
    noise = []
    for _ in range(500):
        ask = rnd.uniform(0.6, 0.92)
        noise.append(({"ask": ask, "left": rnd.uniform(60, 700), "dip_90": rnd.uniform(0, 0.3), "imbalance": rnd.uniform(-1, 1), "imbalance_x_side": 0.0, "spread": 0.01, "elapsed_min": 8.0}, 1 if rnd.random() < ask else 0))
    m = contextscore.fit(noise, lam=300.0)
    for ask in (0.65, 0.85):
        assert abs(contextscore.predict(m, {"ask": ask, "left": 300.0, "dip_90": 0.1, "imbalance": 0.0, "spread": 0.01, "elapsed_min": 8.0}) - ask) < 0.08
    assert contextscore.fit(rows_with_signal(50)) is None
    assert 0.0 < contextscore.predict(m, {"ask": 0.85, "left": 2.4e9}) < 1.0  # a reading far outside the range cannot overflow it


def test_unit_the_step_is_shadow_it_never_says_no():
    assert "context_score" in lanes.BUY_STEPS and "context_score" in buychain.STEPS and "context_score" not in buychain.NEEDS_ESTIMATE
    from kalshi_moonshot.strategy import LongShot

    shot = LongShot(ticker="KXGOLD15M-26NOV190830-30", title="t", rules="", bid=0.88, ask=0.90, volume=0.0, close_time="2026-11-19T13:30:00Z", hours_to_close=0.1)
    assert buychain.context_score_step(buychain.Ctx(s=None, spec=None), shot).ok


def test_unit_a_shadow_step_in_the_chain_does_not_change_the_lane_version(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = config.load()
    mk = lambda **kw: lanes.LaneSpec("x", "ladder", "x", (0.70, 0.97), (0, 1), 0.05, 0.0, **kw)
    assert lanes.settings_of(mk(buy_chain=("price", "cushion")), cfg) == lanes.settings_of(mk(buy_chain=("price", "cushion", "context_score")), cfg)


def write_model(tmp_path):
    m = contextscore.fit(rows_with_signal(), lam=10.0)
    (tmp_path / "context_score.json").write_text(json.dumps(m))
    return m


def test_integration_a_bet_row_gets_the_note_with_the_chance_the_shortfall_and_the_percentile(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    write_model(tmp_path)
    store = Store(config.load().db_path)
    factors = json.dumps({"elapsed_min": 9.0, "spread": 0.01, "imbalance": -0.5, "move_90": 0.05, "range_90": 0.2, "activity_60": 1200.0, "rise_30": 0.0, "rise_90": 0.02, "dip_90": 0.2, "held_min": 5.0, "updates_60": 50})
    store.add_decision(ticker="KXGOLD15M-26NOV190830-30", action="bet", mode="trade", side="no", ask=0.90, bid=0.88, lane="commodity_precious", close_time="2099-01-01T00:15:00Z", reason="bought NO", factors=factors, _context_score=True)
    row = store.db.execute("SELECT reason FROM decisions WHERE ticker='KXGOLD15M-26NOV190830-30'").fetchone()
    assert row["reason"].startswith("bought NO") and "[context score (shadow): wins " in row["reason"] and "against the ask 0.90" in row["reason"] and "th percentile of 400 training bets]" in row["reason"]


def test_integration_without_the_flag_or_without_a_model_the_row_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    store = Store(config.load().db_path)
    store.add_decision(ticker="A-1-1", action="bet", mode="trade", side="yes", ask=0.8, bid=0.79, lane="x", close_time="2099-01-01T00:15:00Z", reason="bought YES", _context_score=True)  # no model yet
    write_model(tmp_path)
    store.add_decision(ticker="B-1-1", action="bet", mode="trade", side="yes", ask=0.8, bid=0.79, lane="x", close_time="2099-01-01T00:15:00Z", reason="bought YES")  # not asked
    store.add_decision(ticker="C-1-1", action="pass", mode="trade", side="yes", ask=0.8, bid=0.79, lane="x", close_time="2099-01-01T00:15:00Z", reason="no", _context_score=True)  # not a bet
    got = {r["ticker"]: r["reason"] for r in store.db.execute("SELECT ticker, reason FROM decisions").fetchall()}
    assert got == {"A-1-1": "bought YES", "B-1-1": "bought YES", "C-1-1": "no"}


def test_integration_the_step_in_a_lane_files_buy_module_marks_the_bets_the_runner_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text("[module.m]\nsteps=['price','context_score']\n\n[lane.a]\nestimator='market'\nprice=[0.10,0.95]\nhours=[0.0,1.0]\nsides=['yes','no']\nmax_spread=0.05\nmin_volume=0\nlabel='a'\nbuy_module='m'\n")
    s = config.load()
    spec = lanes.lane_specs(s)["a"]
    assert spec.buy_chain == ("price", "context_score")


def test_integration_the_runner_logs_a_bet_of_a_lane_with_the_step_with_the_note_and_a_lane_without_it_plain(tmp_path, monkeypatch):
    from kalshi_moonshot import runner
    from kalshi_moonshot.strategy import LongShot

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    base = "estimator='market'\nprice=[0.10,0.95]\nhours=[0.0,1.0]\nsides=['yes','no']\nmax_spread=0.05\nmin_volume=0\n"
    (tmp_path / "strategies.toml").write_text(f"[module.m]\nsteps=['price','context_score']\n[module.plain]\nsteps=['price']\n\n[lane.a]\n{base}label='a'\nbuy_module='m'\n\n[lane.b]\n{base}label='b'\nbuy_module='plain'\n")
    write_model(tmp_path)
    s = config.load()
    store = Store(s.db_path)
    for lane in ("a", "b"):
        shot = LongShot(ticker=f"KXGOLD15M-26NOV190830-30{lane}", title="t", rules="", bid=0.88, ask=0.90, volume=0.0, close_time="2099-01-01T00:15:00Z", hours_to_close=0.1, lane=lane, side="no")
        runner._log(store, s, shot, action="bet", side="no", reason="bought NO", factors=json.dumps({"dip_90": 0.2, "imbalance": -0.5}))
    got = {r["lane"]: r["reason"] for r in store.db.execute("SELECT lane, reason FROM decisions").fetchall()}
    assert "[context score (shadow)" in got["a"] and got["b"] == "bought NO"
