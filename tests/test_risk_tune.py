"""The risk score's tune (risktune.py; lane field risk_score_tuned): 2026-11-16, user: "Make sure the logic for this risk factor is well defined and can be auto tuned to match the changing markets"; "Yea do it"."""
import json
import random
import sys
import time

import pytest

sys.path.insert(0, "tests")
import test_risk_score as T  # noqa: E402

from kalshi_moonshot import config, riskscore, risktune  # noqa: E402
from kalshi_moonshot.store import Store  # noqa: E402


@pytest.fixture(autouse=True)
def clean():
    riskscore._cache.update(data=None, mtime=-1.0, checked=-1e9, dir=None)
    yield
    riskscore._cache.update(data=None, mtime=-1.0, checked=-1e9, dir=None)


def rd(gap, k=0.60, ask=0.61, falling=False):
    ks = [k, k, k + 0.05] if falling else [k, k, k]
    return {"x": [0.0] * 15, "c": [x + gap for x in ks], "k": ks, "ask": ask}


def test_unit_the_gap_is_the_least_lead_and_a_falling_side_has_none():
    assert risktune.held_gap({"c": [0.66, 0.64, 0.65], "k": [0.60, 0.60, 0.60]}) == pytest.approx(0.04)
    assert risktune.held_gap(rd(0.04, falling=True)) is None
    assert risktune.held_gap({"c": [], "k": []}) is None
    assert risktune.step_of(-0.10) == 0 and risktune.step_of(0.035) == risktune.STEPS.index(0.03) + 1 and risktune.step_of(0.5) == len(risktune.STEPS)


def test_unit_a_market_counts_once_per_step_at_its_first_reading_and_the_fee_is_charged():
    rows = [("M-1", "yes", 10.0, True, rd(0.035, ask=0.50)), ("M-1", "yes", 20.0, True, rd(0.036, ask=0.90)), ("M-2", "yes", 5.0, False, rd(0.035, ask=0.50))]
    t = risktune.gap_table(rows)[risktune.step_of(0.035)]
    assert t["n"] == 2 and t["won"] == 1
    assert t["net"] == pytest.approx(((1 / (0.50 + 0.07 * 0.25) - 1) + (-1)) / 2, abs=1e-3)  # M-1 at its first ask (0.50), not its later 0.90


def test_unit_the_range_is_the_run_of_paying_steps_with_enough_markets():
    def tab(nets):
        return [{"lo": None if i == 0 else risktune.STEPS[i - 1], "hi": None if i == len(risktune.STEPS) else risktune.STEPS[i], "n": n, "shrunk": sh} for i, (n, sh) in enumerate(nets)]

    quiet = [(50, -0.02)] * (len(risktune.STEPS) + 1)
    assert risktune.best_range(tab(quiet)) is None
    i3 = risktune.STEPS.index(0.03) + 1
    paying = list(quiet)
    paying[i3], paying[i3 + 1] = (40, 0.03), (30, 0.02)
    assert risktune.best_range(tab(paying)) == (0.03, 0.05)
    thin = list(quiet)
    thin[i3] = (risktune.MIN_N - 1, 0.50)
    assert risktune.best_range(tab(thin)) is None  # too few markets, however good
    edge = list(quiet)
    edge[-1] = (500, 0.50)
    assert risktune.best_range(tab(edge)) is None  # the open-ended step is never bought


def test_unit_a_refit_is_taken_only_when_it_is_no_worse_held_out(monkeypatch):
    rng = random.Random(7)
    monkeypatch.setattr(risktune, "MIN_FIT", 400)
    rows = []
    for i in range(1500):
        x = [rng.uniform(-2, 2)] + [rng.uniform(-1, 1)] + [0.0] * 13
        p = 1 / (1 + 2.718281828 ** -(1.0 * x[0] + 0.8 * x[1]))
        rows.append((f"M-{i}", float(i * 40), rng.random() < p, x))
    wrong = [1.0, -0.8] + [0.0] * 13
    new, info = risktune.refit(rows, wrong)
    assert new is not None and new[1] > 0.4 and info["loss_refit"] < info["loss_in_use"]
    right = [1.0, 0.8] + [0.0] * 13
    noise = [(t, ts, rng.random() < 0.5, x) if ts <= 0.8 * 1500 * 40 else (t, ts, w, x) for t, ts, w, x in rows]  # a training stretch that would mislead
    kept, info = risktune.refit(noise, right)
    assert kept is None and "worse held out" in info["why"]
    assert risktune.refit(rows[:100], right)[0] is None  # too few readings


def seed(tmp_path, monkeypatch, n=60, gap=0.035, win_every=1, tuned="true"):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "strategies.toml").write_text(T.LANE.format(extra=f"risk_score_buy=1.0\nrisk_score_gap_max=0.06\nrisk_score_gap_min=0.03\nrisk_score_tuned={tuned}"))
    s = config.load()
    store = Store(s.db_path)
    now = time.time()
    for i in range(n):
        store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, outcome, factors) VALUES (?,?,?,?,?,?,?,?,?)",
                         (now - 3600 + i, f"KXBTC15M-{i}", "pass", "observe", 0.61, "yes", "rs", "yes" if i % win_every == 0 else "no", json.dumps({"spread": 0.01, "risk": rd(gap)})))
    store.db.commit()
    return s


def test_integration_the_tune_reads_the_lanes_rows_and_the_lane_buys_the_records_gap(tmp_path, monkeypatch):
    s = seed(tmp_path, monkeypatch)  # sixty markets at a 3.5 point gap, bought at 0.61, all won
    out = risktune.rebuild(s)
    assert out["lanes"] == ["rs"] and out["readings"] == 60 and out["gap"] == [0.03, 0.04]
    riskscore._cache["checked"] = -1e9  # the bot looks at the file once every 30 s; the tune runs in another process
    assert riskscore.tuned_gap(str(s.data_dir)) == (0.03, 0.04) and riskscore.weights(str(s.data_dir)) == pytest.approx(riskscore.WEIGHTS, abs=1e-5)
    monkeypatch.setattr(riskscore, "live", lambda *a, **k: ([0.65, 0.65, 0.65], 0.615, [0.615, 0.615, 0.615], tuple([0.0] * 15)))  # a 3.5 point gap: inside the record's range
    extra = "risk_score_buy=1.0\nrisk_score_gap_max=0.06\nrisk_score_gap_min=0.03\nrisk_score_tuned=true"
    out = keep_file(tmp_path, monkeypatch, extra, ask=0.62)
    assert out.action == "bet"
    monkeypatch.setattr(riskscore, "live", lambda *a, **k: ([0.665, 0.665, 0.665], 0.615, [0.615, 0.615, 0.615], tuple([0.0] * 15)))  # 5 points: inside the lane's own 3 to 6, outside the record's 3 to 4
    out = keep_file(tmp_path, monkeypatch, extra, ask=0.62)
    assert out.action == "pass" and "the record's least gap" in out.note


def keep_file(tmp_path, monkeypatch, extra, ask):
    """T.decide with the tune's file left in place (decide writes the lane file into the same folder)."""
    riskscore._cache["checked"] = -1e9
    return T.decide(tmp_path, monkeypatch, time.time(), extra, ask=ask)


def test_integration_when_no_gap_paid_the_tuned_lane_buys_nothing_and_an_untuned_lane_keeps_its_own(tmp_path, monkeypatch):
    s = seed(tmp_path, monkeypatch, win_every=3)  # a third won at 0.61: every step loses
    assert risktune.rebuild(s)["gap"] is None
    monkeypatch.setattr(riskscore, "live", lambda *a, **k: ([0.66, 0.66, 0.66], 0.615, [0.615, 0.615, 0.615], tuple([0.0] * 15)))
    base = "risk_score_buy=1.0\nrisk_score_gap_max=0.06\nrisk_score_gap_min=0.03"
    out = keep_file(tmp_path, monkeypatch, base + "\nrisk_score_tuned=true", ask=0.62)
    assert out.action == "pass" and "shows no gap over Kalshi's price that paid" in out.note
    out = keep_file(tmp_path, monkeypatch, base, ask=0.62)
    assert out.action == "bet"  # not tuned: the lane's own 3 to 6 points


def test_integration_with_no_tune_yet_the_tuned_lane_uses_its_own_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(riskscore, "live", lambda *a, **k: ([0.66, 0.66, 0.66], 0.615, [0.615, 0.615, 0.615], tuple([0.0] * 15)))
    out = T.decide(tmp_path, monkeypatch, time.time(), "risk_score_buy=1.0\nrisk_score_gap_max=0.06\nrisk_score_gap_min=0.03\nrisk_score_tuned=true", ask=0.62)
    assert out.action == "bet"


def test_integration_every_evaluation_stores_its_reading_on_the_decision_row(tmp_path, monkeypatch):
    out = T.decide(tmp_path, monkeypatch, time.time(), "risk_score_buy=0.25\nrisk_score_edge=-0.05")  # real readings, seeded index and mids
    assert out.action == "bet"
    row = Store(config.load().db_path).db.execute("SELECT factors FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    got = json.loads(row["factors"])["risk"]
    assert len(got["x"]) == len(riskscore.NAMES) and len(got["c"]) == 3 and len(got["k"]) == 3 and got["ask"] == pytest.approx(0.81)
    assert risktune.held_gap(got) is not None
