from kalshi_moonshot import config, watcher
from kalshi_moonshot.store import Store


def make(tmp_path, monkeypatch, answer, **env):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = config.load()
    asked = []

    def model(ticker, side, bid, close):
        asked.append((ticker, side))
        if isinstance(answer, Exception):
            raise answer
        return answer

    w = watcher.ProfitWatcher(lambda: s, None, lambda: Store(tmp_path / "m.db"), stop_model=model)
    w._back_off = lambda *a: None
    return s, w, asked


def test_the_stop_holds_when_the_model_values_the_position_well_above_the_sale(tmp_path, monkeypatch):
    s, w, asked = make(tmp_path, monkeypatch, 0.60)
    assert w._model_holds_stop(s, "KXBTC15M-A-00", "x", "yes", 0.45, 0.30, "") and asked == [("KXBTC15M-A-00", "yes")]


def test_the_stop_sells_when_the_model_agrees_is_unsure_or_fails(tmp_path, monkeypatch):
    for answer in (0.28, None, RuntimeError("slow")):
        s, w, _ = make(tmp_path, monkeypatch, answer)
        assert not w._model_holds_stop(s, "KXBTC15M-A-00", "x", "yes", 0.45, 0.30, "")


def test_a_favorite_the_model_now_sees_as_a_loser_is_sold_even_if_it_is_priced_above_the_bid(tmp_path, monkeypatch):
    s, w, _ = make(tmp_path, monkeypatch, 0.45)
    assert not w._model_holds_stop(s, "KXBTC15M-A-00", "x", "yes", 0.70, 0.30, "")


def test_only_15_minute_windows_and_a_nonzero_wait_ask_the_model(tmp_path, monkeypatch):
    s, w, asked = make(tmp_path, monkeypatch, 0.9)
    assert not w._model_holds_stop(s, "KXBTCD-A-00", "x", "yes", 0.45, 0.30, "") and asked == []
    s, w, asked = make(tmp_path, monkeypatch, 0.9, STOP_MODEL_SECONDS="0")
    assert not w._model_holds_stop(s, "KXBTC15M-A-00", "x", "yes", 0.45, 0.30, "") and asked == []


def test_it_is_off_unless_a_lane_or_the_hand_placed_setting_turns_it_on(tmp_path, monkeypatch):
    s, w, _ = make(tmp_path, monkeypatch, 0.9)
    assert not s.manual_stop_model and not w._stop_model_lane("crypto_favorite")


def test_hold_chance_returns_the_chance_the_side_pays_and_none_when_the_model_is_too_slow(tmp_path, monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor

    from kalshi_moonshot.estimators import LLMShapeEstimator
    from kalshi_moonshot.strategy import LongShot

    s = config.load()

    class Model:
        def __init__(self, delay):
            self.delay = delay

        def ask(self, prompt, tokens, tag):
            time.sleep(self.delay)
            return 0.8, "x"

    shot = LongShot(ticker="KXBTC15M-A-00", title="t", rules="", bid=0.3, ask=0.32, volume=0.0, close_time="", hours_to_close=0.0, side="no")
    for delay, want in ((0.0, 0.2), (0.5, None)):
        est = LLMShapeEstimator(Model(delay), Store(tmp_path / "m.db"), ThreadPoolExecutor(max_workers=1))
        est._prompt = lambda shot, s, now: ("prompt", 5)
        got = est.hold_chance(s, shot, timeout=0.2)
        assert (got is None) if want is None else abs(got - want) < 1e-9


def test_the_first_hold_is_soft_and_only_a_second_hold_in_a_row_is_firm(tmp_path, monkeypatch):
    s, w, _ = make(tmp_path, monkeypatch, 0.60, STOP_MODEL_RECHECK_SECONDS="15")
    waits = []
    w._back_off = lambda ticker, lane, seconds=None: waits.append(seconds)
    args = (s, "KXBTC15M-A-00", "x", "yes", 0.45, 0.30, "")
    assert w._model_holds_stop(*args) and waits == [15.0]  # soft: look again soon
    assert w._model_holds_stop(*args) and waits == [15.0, None]  # confirmed: the usual rest


def test_a_sell_answer_after_a_soft_hold_sells_and_forgets_the_hold(tmp_path, monkeypatch):
    s, w, _ = make(tmp_path, monkeypatch, 0.60)
    args = (s, "KXBTC15M-A-00", "x", "yes", 0.45, 0.30, "")
    assert w._model_holds_stop(*args)
    w.stop_model = lambda *a: 0.20  # the price has moved on and the model changes its mind
    assert not w._model_holds_stop(*args) and not w._soft_holds
