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


# 2026-11-17, KXBTC15M-26NOV171845: the live crypto lane bought NO at 0.59 on a "model chance" of 0.72 that was NO's own price a minute earlier, read by a paper lane that bets the price band and handed on through this share. A price-band estimate is the asking lane's ask, not a reading of the market, so it is never kept.
def test_a_lanes_own_price_is_never_handed_to_another_lane_as_the_models_chance():
    share = EstimateShare(Clock())
    band = share.get("ladder", shot(lane="bets_the_price"), 60, lambda: Estimate(0.72, "price band"))  # a lane that needs no model: its "estimate" is its own ask
    assert band.p == 0.72
    model = share.get("ladder", shot(lane="needs_the_model"), 60, lambda: Estimate(0.55, "spot 1 (0s old) -> P(yes) 0.55"))  # the next lane, seconds later, on the same market
    assert model.p == 0.55 and "P(yes)" in model.why  # it got the ladder's own number, not the other lane's price
    assert share.hits == 0


def test_a_real_estimate_is_still_shared_and_a_price_band_ask_does_not_replace_it():
    share, calls = EstimateShare(Clock()), []

    def ladder():
        calls.append(1)
        return Estimate(0.55, "spot 1 (0s old) -> P(yes) 0.55")

    assert share.get("ladder", shot(lane="a"), 60, ladder).p == 0.55
    assert share.get("ladder", shot(lane="b"), 60, ladder).p == 0.55 and len(calls) == 1  # shared as before


def test_integration_a_lane_that_needs_the_model_does_not_buy_on_another_lanes_price(tmp_path, monkeypatch):
    """Through runner.evaluate with one share: a lane that bets the price band reads NO at 0.72; a minute's fall later a lane that needs the model is asked about NO at 0.59. It must be given its own estimator's chance (0.60 here), which has no edge, not the 0.72."""
    import time

    from kalshi_moonshot import regime, runner
    from kalshi_moonshot.store import Store

    class Ladder:
        def estimate(self, client, settings, sh):
            from kalshi_moonshot import lanes

            if not lanes.spec_for(settings, sh.lane).needs_model_edge:  # as commodities.CommodityPricer.estimate does for a lane that bets the price
                return Estimate(1.0 - sh.ask if sh.side == "no" else sh.ask, "price band")
            return Estimate(0.40, "spot 1 (0s old) -> P(yes) 0.40")  # NO worth 0.60

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    base = "estimator='market'\nprice=[0.05,0.95]\nhours=[0.0,1.0]\nsides=['yes','no']\nfixed_stake=1.0\nseries=['KXBTC15M']\n"
    (tmp_path / "strategies.toml").write_text(f"[lane.band]\n{base}needs_model_edge=false\n\n[lane.model]\n{base}needs_model_edge=true\nmin_edge_room=0.12\n")
    s = config.load()
    store = Store(s.db_path)
    regime.reset()
    now = time.time()
    store.db.execute("INSERT OR REPLACE INTO windows (ticker, series, close_ts, strike, fetched_ts) VALUES (?,?,?,?,?)", ("KXBTC15M-7", "KXBTC15M", now + 400.0, 100.0, now))
    store.db.commit()
    share, est = EstimateShare(), {"market": Ladder()}
    share.UNSHARED = frozenset()  # this test's estimator is filed under "market", which is never shared; the live one is "ladder", which is

    def ask_about(lane, bid, ask):
        sh = LongShot("KXBTC15M-7", "t", "", bid, ask, 400.0 / 3600.0, "2099-01-01T00:00:00Z", 400.0 / 3600.0, lane=lane, series="KXBTC15M", side="no")
        return runner.evaluate(s, None, None, store, sh, estimators=est, share=share)

    ask_about("band", 0.71, 0.72)
    out = ask_about("model", 0.58, 0.59)
    assert out.action == "pass" and "price band" not in out.note and "P(yes) 0.40" in out.note, out.note
