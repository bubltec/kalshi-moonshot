import pytest

from kalshi_moonshot import config
from kalshi_moonshot.estimators import Estimate, EstimateShare
from kalshi_moonshot.strategy import LongShot


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def shot(ticker="KX-1", lane="a"):
    return LongShot(ticker=ticker, title="t", rules="", bid=0.38, ask=0.4, volume=0.0, close_time="2026-10-25T01:00:00Z", hours_to_close=0.2, lane=lane, category="Crypto")


def test_many_lanes_asking_about_one_market_make_one_estimate():
    share, calls = EstimateShare(Clock()), []

    def compute():
        calls.append(1)
        return Estimate(0.6, "model")

    got = [share.get("llm", shot(lane=f"lane{i}"), 60, compute) for i in range(100)]
    assert len(calls) == 1 and all(g.p == 0.6 for g in got) and share.hits == 99


def test_an_estimate_is_asked_again_once_it_is_older_than_the_window_or_for_another_market():
    clock, calls = Clock(), []
    share = EstimateShare(clock)

    def compute():
        calls.append(1)
        return Estimate(0.5, "x")

    share.get("llm", shot("A"), 60, compute)
    share.get("llm", shot("B"), 60, compute)
    clock.t += 61
    share.get("llm", shot("A"), 60, compute)
    assert len(calls) == 3


def test_each_estimator_keeps_its_own_answer_for_a_market():
    share = EstimateShare(Clock())
    a = share.get("llm", shot(), 60, lambda: Estimate(0.2, "llm"))
    b = share.get("ladder", shot(), 60, lambda: Estimate(0.9, "ladder"))
    assert (a.p, b.p) == (0.2, 0.9)


def test_the_market_price_estimator_and_a_zero_window_are_never_shared():
    share, calls = EstimateShare(Clock()), []

    def compute():
        calls.append(1)
        return Estimate(0.5, "x")

    for _ in range(3):
        share.get("market", shot(), 60, compute)
        share.get("llm", shot(), 0, compute)
    assert len(calls) == 6


def test_a_failure_is_not_kept_so_the_next_lane_tries_again():
    share, calls = EstimateShare(Clock()), []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("model down")
        return Estimate(0.7, "ok")

    with pytest.raises(RuntimeError):
        share.get("llm", shot(), 60, flaky)
    assert share.get("llm", shot(), 60, flaky).p == 0.7 and len(calls) == 2


def test_the_window_is_a_setting():
    assert config.load().estimate_share_seconds == 60.0
