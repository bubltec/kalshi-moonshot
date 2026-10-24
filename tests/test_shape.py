from datetime import UTC, datetime

import pytest

from kalshi_moonshot import shape
from kalshi_moonshot.estimators import EstimateFailed, LLMShapeEstimator
from kalshi_moonshot.strategy import LongShot

CLOSE = 1_800_000_900.0
OPEN = CLOSE - 900.0


def candle_rows(mids, close_ts=CLOSE):
    return [(close_ts - 900 + 60 * (i + 1), m - 0.005, m + 0.005) for i, m in enumerate(mids)]


def window(tk, mids, result, close_ts=CLOSE):
    return shape.Window(tk, close_ts, result, shape.path_from_candles(candle_rows(mids, close_ts), close_ts))


def test_candles_become_a_fifteen_minute_shape():
    p = shape.path_from_candles(candle_rows([0.5, 0.6, 0.7]), CLOSE)
    assert len(p) == 15 and p[:3] == [pytest.approx(0.5), pytest.approx(0.6), pytest.approx(0.7)] and p[3] is None


def test_quote_ticks_become_the_mid_at_each_minute_mark():
    quotes = [(OPEN + 10, 0.49, 0.51), (OPEN + 70, 0.59, 0.61), (OPEN + 130, 0.69, 0.71)]
    p = shape.path_from_quotes(quotes, OPEN, 2)
    assert p == [pytest.approx(0.50), pytest.approx(0.60)]  # minute 1 ends at +60 s: the +10 quote; minute 2 at +120 s: the +70 quote


def test_the_nearest_windows_are_the_ones_with_the_closest_shape():
    cur = [0.5, 0.6, 0.7, 0.8]
    pool = [window("a", [0.5, 0.6, 0.7, 0.8] + [0.9] * 11, "yes"), window("b", [0.5, 0.4, 0.3, 0.2] + [0.1] * 11, "no"), window("c", [0.5, 0.62, 0.7, 0.78] + [0.9] * 11, "yes")]
    got = shape.nearest(cur, pool, 4, k=2)
    assert [n.window.ticker for n in got] == ["a", "c"] and got[0].distance < got[1].distance


def test_a_window_missing_most_of_the_stretch_is_not_compared():
    thin = shape.Window("t", CLOSE, "yes", [0.5] + [None] * 14)
    assert shape.distance([0.5, 0.6, 0.7, 0.8], thin.path, 4) is None


def test_the_plain_rate_counts_windows_at_a_similar_price():
    pool = [window(f"w{i}", [0.5, 0.6, 0.7, 0.8] + [0.9] * 11, "yes" if i < 3 else "no") for i in range(4)]
    n, rate = shape.rate_near(pool, 4, 0.80, 0.05)
    assert n == 4 and rate == 0.75
    assert shape.rate_near(pool, 4, 0.20, 0.05) == (0, None)


def test_the_prompt_shows_the_shape_the_neighbours_and_the_rates_but_asks_for_json():
    cur = [0.5, 0.6, 0.7, 0.8]
    pool = [window(f"w{i}", [0.5, 0.6, 0.7, 0.8] + [0.9] * 11, "yes") for i in range(5)]
    text = shape.build_prompt("BTC price up in next 15 mins?", 5, 0.80, 0.82, cur, shape.nearest(cur, pool, 4), pool)
    assert "minute 5 of 15" in text and "bid 80, ask 82" in text and "50 60 70 80" in text
    assert "| YES" in text and "closest past windows" in text and "all windows: 100%" in text
    assert "90 90 90 90" in text or "90 | 90" in text or "| 90 90 90" in text  # the later checkpoints, in whole cents
    assert '"probability"' in text and "at most 12 words" in text and "0.8" not in text.split("This window so far")[1][:40]


def test_the_prompt_stays_small_with_a_full_set_of_neighbours():
    cur = [0.5, 0.6, 0.7, 0.8, 0.75, 0.7, 0.72, 0.74]
    pool = [window(f"w{i}", [0.5 + 0.01 * i, 0.6, 0.7, 0.8, 0.75, 0.7, 0.72, 0.74] + [0.9] * 7, "yes") for i in range(40)]
    text = shape.build_prompt("BTC price up in next 15 mins?", 9, 0.80, 0.82, cur, shape.nearest(cur, pool, 8), pool)
    assert len(shape.nearest(cur, pool, 8)) == 5 and len(text) < 2100


class SyncExec:
    def submit(self, fn, *a):
        from concurrent.futures import Future

        f = Future()
        try:
            f.set_result(fn(*a))
        except Exception as exc:
            f.set_exception(exc)
        return f


class FakeModel:
    def __init__(self, answer=(0.7, "windows like this mostly settled YES")):
        self.answer, self.prompts = answer, []

    def ask(self, prompt, max_tokens=None, tag=""):
        self.prompts.append(prompt)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def shot(side="yes"):
    return LongShot("KXBTC15M-X", "BTC price up in next 15 mins?", "", 0.78, 0.80, 10, datetime.fromtimestamp(CLOSE, UTC).isoformat(), 0.1, series="KXBTC15M", side=side)


def estimator(model, monkeypatch, pool_size=40):
    est = LLMShapeEstimator(model, store=None, executor=SyncExec())
    pool = [window(f"w{i}", [0.5, 0.6, 0.7, 0.8] + [0.9] * 11, "yes" if i % 2 else "no") for i in range(pool_size)]
    monkeypatch.setattr(est, "_pool", lambda series, now_ts: pool)
    monkeypatch.setattr(est, "_quotes", lambda s, tk, open_ts, now_ts: [(open_ts + 55, 0.49, 0.51), (open_ts + 115, 0.59, 0.61), (open_ts + 175, 0.69, 0.71), (open_ts + 235, 0.79, 0.81)])
    return est


NOW = datetime.fromtimestamp(OPEN + 5 * 60 + 20, UTC)  # 20 s into minute 6, so five minutes are complete


def test_a_market_is_dropped_the_first_pass_and_kept_once_the_answer_is_back(monkeypatch):
    model = FakeModel()
    est = estimator(model, monkeypatch)
    # with a synchronous executor the answer is ready by the time screen returns, but it is only used from the next look
    est.screen([shot()], [], None, None, NOW)
    kept = est.screen([shot()], [], None, None, NOW)
    assert len(model.prompts) == 1 and [x.ticker for x in kept] == ["KXBTC15M-X"]
    got = est.estimate(None, None, shot())
    assert got.p == 0.7 and "windows like this" in got.why and "minute 6 of 15" in got.context


def test_nothing_is_asked_too_early_or_with_too_little_history(monkeypatch):
    model = FakeModel()
    est = estimator(model, monkeypatch)
    est.screen([shot()], [], None, None, datetime.fromtimestamp(OPEN + 90, UTC))  # one minute done
    assert model.prompts == []
    est2 = estimator(model, monkeypatch, pool_size=5)
    est2.screen([shot()], [], None, None, NOW)
    assert model.prompts == []


def test_a_failing_model_trades_nothing_and_is_not_asked_again_at_once(monkeypatch):
    model = FakeModel(RuntimeError("ollama is down"))
    est = estimator(model, monkeypatch)
    est.screen([shot()], [], None, None, NOW)
    assert est.screen([shot()], [], None, None, NOW) == []
    assert len(model.prompts) == 1  # the second look waits out the pause
    with pytest.raises(EstimateFailed):
        est.estimate(None, None, shot())


def test_the_yes_and_no_sides_of_one_market_share_one_model_call(monkeypatch):
    model = FakeModel()
    est = estimator(model, monkeypatch)
    est.screen([shot("yes"), shot("no")], [], None, None, NOW)
    assert len(model.prompts) == 1
