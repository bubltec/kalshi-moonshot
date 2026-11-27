"""One lane places a buy on a market at a time (runner.claim / release; 2026-11-27, user: "Each lane should only look at what it has placed").

The risk gate refuses a market the bot's orders already hold, but an order is on the books only when it is done: two lanes could each rest a buy on one market for some seconds and both fill. The claim is taken by runner.place from before the
risk gate until the order's row is written. It goes with the lane isolation of the same day (store.lane_contracts, net_contracts by side)."""
import threading

import pytest

from kalshi_moonshot import config, runner
from kalshi_moonshot.store import Store

T = "KXNEAR15M-26NOV270645-45"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LIVE_LANES", "priskp,wave_shape")
    s = config.load()
    return s, Store(s.db_path)


# ---- the claim: one lane places a buy on a market at a time ----
def test_unit_the_second_lane_is_told_which_lane_holds_the_market_and_the_same_lane_may_ask_again():
    runner._CLAIMS.clear()
    assert runner.claim(T, "priskp") is None
    assert runner.claim(T, "priskp") is None  # the lane's own second step (a top-up, a second try)
    assert runner.claim(T, "wave_shape") == "priskp"
    assert runner.claim("KXBTC15M-X", "wave_shape") is None  # another market is free
    runner.release(T, "wave_shape")  # a lane cannot release another's claim
    assert runner.claim(T, "wave_shape") == "priskp"
    runner.release(T, "priskp")
    assert runner.claim(T, "wave_shape") is None
    runner._CLAIMS.clear()


def test_integration_a_buy_is_refused_while_another_lane_is_placing_one_on_the_market_and_the_claim_is_given_back(env, monkeypatch):
    from kalshi_moonshot.strategy import LongShot

    s, store = env
    runner._CLAIMS.clear()
    shot = LongShot(ticker=T, title="", rules="", bid=0.35, ask=0.36, volume=0.0, close_time="2099-01-01T00:00:00Z", hours_to_close=0.1, side="yes", lane="wave_shape", event_ticker=T.rsplit("-", 1)[0])
    assert runner.claim(T, "priskp") is None  # PriskP's resting buy is waiting on this market
    out = runner.place(s, None, store, shot, "test", 5.55)
    assert out.filled == 0 and "the lane priskp is placing a buy on this market right now" in out.note
    assert runner._CLAIMS == {T: "priskp"}  # the refused lane did not take or drop the claim
    runner.release(T, "priskp")
    seen = {}

    def inner(*a, **k):
        seen["claimed"] = dict(runner._CLAIMS)
        raise RuntimeError("the order path failed")

    monkeypatch.setattr(runner, "_place", inner)
    with pytest.raises(RuntimeError):
        runner.place(s, None, store, shot, "test", 5.55)
    assert seen["claimed"] == {T: "wave_shape"} and runner._CLAIMS == {}  # held while the order is placed, given back even when it fails


def test_unit_two_threads_cannot_both_take_one_market():
    runner._CLAIMS.clear()
    got = []
    gate = threading.Barrier(8)

    def go(lane):
        gate.wait()
        got.append((lane, runner.claim(T, lane)))

    threads = [threading.Thread(target=go, args=(f"lane{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for _lane, other in got if other is None) == 1
    runner._CLAIMS.clear()
