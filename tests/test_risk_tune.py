"""The risk score's tune (risktune.py): 2026-11-16, user: "Make sure the logic for this risk factor is well defined and can be auto tuned to match the changing markets"; "Yea do it"."""
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
    (tmp_path / "strategies.toml").write_text(T.LANE.format(extra="risk_score_buy=1.0"))
    s = config.load()
    store = Store(s.db_path)
    now = time.time()
    for i in range(n):
        store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, outcome, factors) VALUES (?,?,?,?,?,?,?,?,?)",
                         (now - 3600 + i, f"KXBTC15M-{i}", "pass", "observe", 0.61, "yes", "rs", "yes" if i % win_every == 0 else "no", json.dumps({"spread": 0.01, "risk": rd(gap)})))
    store.db.commit()
    return s


def test_integration_every_evaluation_stores_its_reading_on_the_decision_row(tmp_path, monkeypatch):
    out = T.decide(tmp_path, monkeypatch, time.time(), "risk_score_buy=0.25")  # real readings, seeded index and mids
    assert out.action == "bet"
    row = Store(config.load().db_path).db.execute("SELECT factors FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
    got = json.loads(row["factors"])["risk"]
    assert len(got["x"]) == len(riskscore.NAMES) and len(got["c"]) == 3 and len(got["k"]) == 3 and got["ask"] == pytest.approx(0.81)


def test_integration_the_tune_reads_the_lanes_rows_and_writes_the_weights(tmp_path, monkeypatch):
    s = seed(tmp_path, monkeypatch)  # sixty settled readings on the lane's own decision rows
    out = risktune.rebuild(s)
    assert out["lanes"] == ["rs"] and out["readings"] == 60 and "gap" not in out and "gap_table" not in out
    riskscore._cache["checked"] = -1e9  # the bot looks at the file once every 30 s; the tune runs in another process
    assert riskscore.weights(str(s.data_dir)) == pytest.approx(riskscore.WEIGHTS, abs=1e-5)  # too few readings for a refit: the weights in use are kept
