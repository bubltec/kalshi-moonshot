import pytest

from kalshi_moonshot import config, lanes
from kalshi_moonshot.estimators import LadderEstimator
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot


class FakePricer:
    def __init__(self, p):
        self.p = p

    def price_markets(self, markets, s, now):
        return {m["ticker"]: (self.p, "note") for m in markets}


def shot(ticker="KXBTCD-X-T1", ask=0.77, side="yes"):
    return LongShot(ticker, "t", "r", ask - 0.01, ask, 100.0, "2026-10-21T01:00:00Z", 0.4, lane="commodity",
                    series="KXBTCD", event_ticker="KXBTCD-X", side=side)


def test_the_screen_records_why_a_market_was_dropped():
    s = config.load()
    spec = lanes.lane_specs_from(s, None)["commodity"]
    est = LadderEstimator(FakePricer(0.65))
    kept = est.screen([shot()], [{"ticker": "KXBTCD-X-T1", "event_ticker": "KXBTCD-X"}], spec, s, None)
    assert kept == []
    [d] = est.dropped
    assert d["ticker"] == "KXBTCD-X-T1" and d["p"] == pytest.approx(0.65) and d["edge"] < 0
    assert "edge" in d["reason"]


def test_a_market_that_clears_the_bar_is_kept_and_not_logged():
    s = config.load()
    spec = lanes.lane_specs_from(s, None)["commodity"]
    est = LadderEstimator(FakePricer(0.95))
    kept = est.screen([shot()], [{"ticker": "KXBTCD-X-T1", "event_ticker": "KXBTCD-X"}], spec, s, None)
    assert [k.ticker for k in kept] == ["KXBTCD-X-T1"] and est.dropped == []


def test_screen_log_keeps_one_row_per_market_and_side_per_gap(tmp_path):
    store = Store(tmp_path / "t.db")
    row = {"ticker": "A", "side": "yes", "ask": 0.7, "p": 0.6, "edge": -0.1, "reason": "x"}
    assert store.log_screened("commodity", [row, {**row, "side": "no"}], now=1000.0) == 2
    assert store.log_screened("commodity", [row], now=1100.0) == 0  # inside the gap
    assert store.log_screened("commodity", [row], now=1400.0) == 1  # after it
    store.prune_screened(days=1.0, now=1000.0 + 3 * 86400)
    assert store.db.execute("SELECT COUNT(*) FROM screen_log").fetchone()[0] == 0
