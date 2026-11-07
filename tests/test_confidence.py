import json
import math
from datetime import datetime
from types import SimpleNamespace

from kalshi_moonshot import confidence


def series(n, start=1000.0, amp=1e-4, drift=0.0, base=100.0):
    ts = [start + i for i in range(n)]
    vs = [base * math.exp(amp * (1 if i % 2 else -1) + drift * i) for i in range(n)]
    return ts, vs


def test_features_come_from_the_curve_in_typical_minutes():
    ts, vs = series(4000)
    f = confidence.features_at(ts, vs, ts[-1], strike=vs[-1] * 1.0003, left=300.0, bid=0.40, ask=0.42)
    assert f and f["z_tm"] < 0 and 0 <= f["pos15"] <= 1 and f["h_strike"] > 1.0 and f["left"] == 5.0
    assert abs(f["logit_mid"] - math.log(0.41 / 0.59)) < 1e-6


def test_short_history_or_a_stale_tick_or_a_bad_quote_has_no_features():
    ts, vs = series(500)
    assert confidence.features_at(ts, vs, ts[-1], 100.0, 300.0, 0.4, 0.42) is None  # under an hour
    ts, vs = series(4000)
    assert confidence.features_at(ts, vs, ts[-1] + 60.0, 100.0, 300.0, 0.4, 0.42) is None  # stale
    assert confidence.features_at(ts, vs, ts[-1], 100.0, 300.0, 0.4, 0.0) is None
    assert confidence.features_at(ts, vs, ts[-1], 100.0, 0.0, 0.4, 0.42) is None


def test_the_ridge_logistic_recovers_a_planted_effect():
    import random

    random.seed(3)
    X, y = [], []
    for _ in range(1500):
        a = random.gauss(0, 1)
        b = random.gauss(0, 1)
        p = 1.0 / (1.0 + math.exp(-(0.3 + 1.5 * a)))
        X.append([1.0, a, b])
        y.append(1 if random.random() < p else 0)
    w = confidence.fit(X, y, ridge=0.1)
    assert 1.2 < w[1] < 1.8 and abs(w[2]) < 0.3 and 0.1 < w[0] < 0.5
    assert confidence.predict(w, [1.0, 2.0, 0.0]) > 0.9


def test_a_row_is_scored_for_buying_more_and_for_selling():
    sig = {"p_side": 0.9, "ask_side": 0.8, "bid_side": 0.78}
    won = confidence.score_row(sig, True)
    lost = confidence.score_row(sig, False)
    assert won["buy_ev"] > 0 and won["sell_gain"] < 0  # holding a winner beat selling it
    assert lost["buy_ev"] < -0.8 and lost["sell_gain"] > 0.7
    assert won["p_error"] < won["ask_error"]  # the model was closer than the price on a win


class Db:
    def __init__(self, bets=None, quote=None, outcome=None):
        self.bets, self.quote, self.outcome = bets or [], quote or [], outcome

    def execute(self, sql, args=()):
        if "FROM quotes" in sql:
            data = self.quote
        elif "outcome IN" in sql:
            data = [self.outcome] if self.outcome else []
        else:
            data = self.bets
        return SimpleNamespace(fetchone=lambda: data[0] if data else None, fetchall=lambda: data)


def test_a_mark_is_logged_once_and_scored_after_settlement(tmp_path, monkeypatch):
    from kalshi_moonshot import marketprofile, ticks

    ts, vs = series(4000)
    close = datetime.fromtimestamp(ts[-1] + 180.0).astimezone().isoformat()
    now = ts[-1]
    monkeypatch.setattr(marketprofile, "_index", lambda s: "IDX")
    monkeypatch.setattr(ticks.history, "window", lambda i, a, b: (ts, vs))
    mu = dict.fromkeys(confidence.NAMES, 0.0)
    sd = dict.fromkeys(confidence.NAMES, 1.0)
    model = {"fitted_at": now, "n": 2000, "mu": mu, "sd": sd, "w": [0.0, 1.0] + [0.0] * len(confidence.NAMES), "names": []}
    monkeypatch.setattr(confidence, "load_model", lambda s: model)
    confidence._ran_at = confidence._scored_at = 0.0
    confidence._seen.clear()
    row = {"ticker": "KXTEST15M-X-00", "lane": "lane_a", "side": "yes", "close_time": close}
    store = SimpleNamespace(open_bets=lambda: [row], db=Db(outcome={"outcome": "yes"}))
    tape = Db(quote=[{"bid": 0.80, "ask": 0.82, "ts": now - 1.0}])
    specs = {"lane_a": SimpleNamespace(confidence_shadow=True)}
    s = SimpleNamespace(data_dir=str(tmp_path))
    got = confidence.shadow_pass(s, store, tape, specs, lambda t: vs[-1], now=now)
    assert len(got) == 1 and got[0]["mark"] == 180 and abs(got[0]["p_side"] - 0.81) < 0.01  # a model that only trusts the price returns the mid
    assert confidence.shadow_pass(s, store, tape, specs, lambda t: vs[-1], now=now + 6.0) == []  # once per mark
    scored = confidence.run_score(s, store, now=now + 3600.0)
    assert len(scored) == 1 and scored[0]["buy_ev"] > 0 and scored[0]["sell_gain"] < 0
    kinds = [json.loads(x)["kind"] for x in (tmp_path / "confidence_shadow.jsonl").read_text().splitlines()]
    assert kinds == ["signal", "score"]


def test_a_lane_without_the_flag_or_a_market_without_a_strike_is_skipped(tmp_path, monkeypatch):
    from kalshi_moonshot import marketprofile, ticks

    ts, vs = series(4000)
    close = datetime.fromtimestamp(ts[-1] + 180.0).astimezone().isoformat()
    monkeypatch.setattr(marketprofile, "_index", lambda s: "IDX")
    monkeypatch.setattr(ticks.history, "window", lambda i, a, b: (ts, vs))
    model = {"fitted_at": 1.0, "n": 2000, "mu": dict.fromkeys(confidence.NAMES, 0.0), "sd": dict.fromkeys(confidence.NAMES, 1.0), "w": [0.0, 1.0] + [0.0] * len(confidence.NAMES)}
    monkeypatch.setattr(confidence, "load_model", lambda s: model)
    row = {"ticker": "KXTEST15M-X-00", "lane": "lane_a", "side": "no", "close_time": close}
    store = SimpleNamespace(open_bets=lambda: [row], db=Db())
    tape = Db(quote=[{"bid": 0.80, "ask": 0.82, "ts": ts[-1] - 1.0}])
    s = SimpleNamespace(data_dir=str(tmp_path))
    confidence._ran_at = 0.0
    confidence._seen.clear()
    assert confidence.shadow_pass(s, store, tape, {"lane_a": SimpleNamespace(confidence_shadow=False)}, lambda t: vs[-1], now=ts[-1]) == []
    confidence._ran_at = 0.0
    assert confidence.shadow_pass(s, store, tape, {"lane_a": SimpleNamespace(confidence_shadow=True)}, lambda t: None, now=ts[-1]) == []
