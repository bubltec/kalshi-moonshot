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


def test_an_always_lane_keeps_a_market_with_no_instrument_instead_of_dropping_it_for_no_feed():
    import dataclasses

    s = config.load()
    spec = dataclasses.replace(lanes.lane_specs_from(s, None)["commodity"], needs_model_edge=False)

    class NoFeed(FakePricer):
        def price_markets(self, markets, s, now):
            return {}

    est = LadderEstimator(NoFeed(0.5))
    sh = dataclasses.replace(shot("KXZEC15M-26NOV072015-15"), series="KXZEC15M", event_ticker="KXZEC15M-26NOV072015")
    kept = est.screen([sh], [{"ticker": sh.ticker, "event_ticker": sh.event_ticker, "series_ticker": "KXZEC15M", "yes_ask_dollars": "0.77"}], spec, s, None)
    assert [k.ticker for k in kept] == [sh.ticker] and est.dropped == []


def test_the_pricer_estimate_for_an_always_lane_with_no_instrument_is_the_price_band(monkeypatch):
    import dataclasses

    from kalshi_moonshot.commodities import CommodityPricer

    s = config.load()
    spec = dataclasses.replace(lanes.lane_specs_from(s, None)["commodity"], needs_model_edge=False)
    monkeypatch.setattr(lanes, "lane_specs", lambda _s: {"band": spec})
    sh = dataclasses.replace(shot("KXZEC15M-26NOV072015-15", ask=0.43), series="KXZEC15M", lane="band")
    p, why = CommodityPricer.__new__(CommodityPricer).estimate(None, sh, s)
    assert abs(p - 0.43) < 1e-9 and why == "price band"


class _Never(FakePricer):
    def price_markets(self, markets, s, now):
        raise AssertionError("a lane that bets the price band must not ask the pricer")


def _btc(ask=0.77):
    import dataclasses

    sh = dataclasses.replace(shot("KXBTC15M-26NOV122100-00", ask=ask), series="KXBTC15M", event_ticker="KXBTC15M-26NOV122100")
    return sh, [{"ticker": sh.ticker, "event_ticker": sh.event_ticker, "series_ticker": "KXBTC15M", "yes_ask_dollars": str(ask)}]


def test_crypto_for_a_price_band_lane_is_still_the_ask_and_never_asks_the_pricer():
    import dataclasses

    s = config.load()
    spec = dataclasses.replace(lanes.lane_specs_from(s, None)["commodity"], needs_model_edge=False)
    sh, markets = _btc()
    est = LadderEstimator(_Never(0.5))
    assert [k.ticker for k in est.screen([sh], markets, spec, s, None)] == [sh.ticker] and est.dropped == []


@pytest.mark.parametrize(("p", "kept"), [(0.95, True), (0.65, False)])
def test_crypto_for_a_lane_that_needs_a_model_is_priced_by_the_pricer(p, kept):
    import dataclasses

    s = config.load()
    spec = dataclasses.replace(lanes.lane_specs_from(s, None)["commodity"], needs_model_edge=True)
    sh, markets = _btc()
    est = LadderEstimator(FakePricer(p))
    assert bool(est.screen([sh], markets, spec, s, None)) is kept


def test_the_decision_time_estimate_for_crypto_follows_the_lane(monkeypatch):
    import dataclasses

    from kalshi_moonshot.commodities import CommodityPricer

    s = config.load()
    base = lanes.lane_specs_from(s, None)["commodity"]
    sh, markets = _btc(ask=0.43)
    sh = dataclasses.replace(sh, lane="x")

    class Client:
        def event_markets(self, event):
            return [dict(m) for m in markets]

    monkeypatch.setattr(CommodityPricer, "price_markets", lambda self, ms, s, now=None: {sh.ticker: (0.81, "perp note")})
    pricer = CommodityPricer.__new__(CommodityPricer)
    monkeypatch.setattr(lanes, "lane_specs", lambda _s: {"x": dataclasses.replace(base, needs_model_edge=False)})
    assert pricer.estimate(None, sh, s) == (pytest.approx(0.43), "price band")  # no client needed: nothing is read
    monkeypatch.setattr(lanes, "lane_specs", lambda _s: {"x": dataclasses.replace(base, needs_model_edge=True)})
    assert pricer.estimate(Client(), sh, s) == (0.81, "perp note")


def test_a_lane_filter_rejection_is_kept_with_its_reason_and_logged_once_per_gate():
    """Every gate says what it did (user, 2026-11-21): the lane's own filter (price, entry window, spread) hands back the sides it turned down, and a review logs each market, side and gate once per gap, so a side stopped by one gate and later by another shows both."""
    from datetime import UTC, datetime, timedelta

    from kalshi_moonshot import runner
    from kalshi_moonshot.lanes import spec_for
    from kalshi_moonshot.strategy import lane_candidates

    s = config.load()
    spec = spec_for(s, "moonshot")
    now = datetime(2026, 11, 21, tzinfo=UTC)
    m = {"ticker": "KX-1", "yes_ask_dollars": "0.99", "yes_bid_dollars": "0.98", "volume_fp": "1000", "close_time": (now + timedelta(hours=1)).isoformat()}
    rejected: list[dict] = []
    assert lane_candidates([m], spec, s, now, rejected) == []
    assert rejected and all(r["reason"].startswith("lane filter: ") for r in rejected)
    assert lane_candidates([m], spec, s, now) == []  # no list handed in: nothing kept

    runner._DROP_SEEN.clear()
    a = {"ticker": "KX-1", "side": "yes", "reason": "lane filter: outside the entry window (1.2 min into a 15-min window)"}
    b = {"ticker": "KX-1", "side": "yes", "reason": "chain price > calm > wave_top: out at wave_top (no wave top)"}
    assert runner._fresh_drops("x", [a], 1000.0) == [a]
    assert runner._fresh_drops("x", [{**a, "reason": a["reason"].replace("1.2", "1.4")}], 1010.0) == []  # the same gate, other numbers
    assert runner._fresh_drops("x", [b], 1020.0) == [b]  # another gate on the same side
    assert len(runner._fresh_drops("x", [a], 1300.0)) == 1  # after the gap
