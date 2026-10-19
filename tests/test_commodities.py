import math
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_moonshot import commodities as cm
from kalshi_moonshot import config, runner
from kalshi_moonshot.estimators import build_estimators
from kalshi_moonshot.lanes import spec_for
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import LongShot

NOW = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
CLOSE = NOW + timedelta(hours=6)


def settings(tmp_path, **over):
    s = config.load()
    base = {
        "data_dir": tmp_path, "mode": "observe", "env": "demo", "skip_categories": frozenset(),
        "commodities_enabled": True, "commodity_min_edge": 0.08, "commodity_max_basis": 0.04,
        "commodity_vol_mult": 1.0, "commodity_min_volume": 0.0, "commodity_min_price": 0.02,
        "commodity_max_price": 0.5, "commodity_max_spread": 0.06,
        "max_usd_per_trade": 5.0, "commodity_usd_per_trade": 2.0, "min_order_usd": 0.0,
    }
    return type(s)(**{**s.__dict__, **base, **over})


def ladder(center=4000.0, sigma=0.01, strikes=range(3900, 4101, 10), quote_sigma=None, series="KXGOLDD"):
    """An event's 'above K' markets quoted from a lognormal with the given width."""
    qs = quote_sigma or sigma
    out = []
    for k in strikes:
        p = cm.prob_above(center, k, qs)
        bid, ask = max(p - 0.01, 0.01), min(p + 0.01, 0.99)
        out.append({
            "ticker": f"{series}-26OCT19-T{k}", "event_ticker": f"{series}-26OCT19",
            "series_ticker": series, "strike_type": "greater", "floor_strike": float(k),
            "cap_strike": None, "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}",
            "volume_fp": "500", "market_type": "binary", "status": "active",
            "close_time": CLOSE.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return out


def feed_series(price=4000.0, per_bar=0.0005, bars=600, age_s=60.0):
    """Alternating +/- moves of exactly per_bar, so realized per-bar sigma is per_bar."""
    end = NOW.timestamp() - age_s
    times = [end - (bars - i) * cm.BAR_SECONDS for i in range(bars + 1)]
    closes, p = [], price
    for i in range(bars + 1):
        closes.append(p)
        p *= math.exp(per_bar if i % 2 == 0 else -per_bar)
    return cm.Series(last=closes[-1], last_ts=times[-1], closes=closes, times=times)


class FakeFeed:
    def __init__(self, series):
        self._series = series

    def series(self, symbol):
        return self._series


def test_symbol_mapping_covers_daily_hourly_weekly_only():
    assert cm.symbol_for("KXGOLDD") == "GC=F"
    assert cm.symbol_for("KXWTIH") == "CL=F"
    assert cm.symbol_for("KXNATGASW") == "NG=F"
    assert cm.symbol_for("KXGOLDMON") is None  # monthly: too long for realized vol
    assert cm.symbol_for("KXWTIMAX") is None
    assert cm.symbol_for("KXNBAGAME") is None


def test_prob_above_is_half_at_center_and_monotone():
    assert cm.prob_above(100, 100, 0.02) == pytest.approx(0.5)
    assert cm.prob_above(100, 98, 0.02) > cm.prob_above(100, 100, 0.02) > cm.prob_above(100, 102, 0.02)
    assert cm.prob_above(100, 100, 0.0) == 0.0
    assert cm.prob_above(100, 99, 0.0) == 1.0


def test_fit_recovers_center_and_width():
    fit = cm.fit_ladder(ladder(center=4000, sigma=0.012))
    assert fit is not None
    assert fit.center == pytest.approx(4000, rel=0.002)
    assert fit.sigma == pytest.approx(0.012, rel=0.1)
    assert fit.r2 > 0.99


def test_fit_needs_enough_liquid_strikes():
    thin = ladder(strikes=[3990, 4000, 4010])
    assert cm.fit_ladder(thin) is None
    wide = ladder()
    for m in wide:
        m["yes_bid_dollars"] = "0.0000"  # no bids anywhere
    assert cm.fit_ladder(wide) is None


def test_market_prob_shapes():
    c, sig = 100.0, 0.02
    above = {"strike_type": "greater", "floor_strike": 101, "cap_strike": None}
    below = {"strike_type": "less", "floor_strike": None, "cap_strike": 101}
    between = {"strike_type": "between", "floor_strike": 99, "cap_strike": 101}
    assert cm.market_prob(above, c, sig) + cm.market_prob(below, c, sig) == pytest.approx(1.0)
    assert 0 < cm.market_prob(between, c, sig) < 0.5
    assert cm.market_prob({"strike_type": "structured"}, c, sig) is None


def test_realized_sigma_scales_with_sqrt_time():
    series = feed_series(per_bar=0.0005)
    one = cm.realized_sigma(series, 1.0)
    four = cm.realized_sigma(series, 4.0)
    assert four == pytest.approx(2 * one, rel=1e-6)
    bars = 1.0 * cm.OPEN_FRACTION * 3600 / cm.BAR_SECONDS
    assert one == pytest.approx(0.0005 * math.sqrt(bars), rel=0.01)


def test_realized_sigma_ignores_session_gaps():
    series = feed_series()
    times = list(series.times)
    for i in range(300, len(times)):
        times[i] += 3 * 86400  # a weekend in the middle
    gapped = cm.Series(series.last, times[-1], series.closes, times)
    assert cm.realized_sigma(gapped, 2.0) == pytest.approx(cm.realized_sigma(series, 2.0), rel=0.01)


def test_price_event_rejects_stale_feed(tmp_path):
    s = settings(tmp_path)
    with pytest.raises(cm.CommodityError, match="old"):
        cm.price_event(ladder(), "GC=F", FakeFeed(feed_series(age_s=3600)), s, NOW)


def test_price_event_rejects_wrong_contract(tmp_path):
    s = settings(tmp_path)
    with pytest.raises(cm.CommodityError, match="different contract"):
        cm.price_event(ladder(center=4000), "GC=F", FakeFeed(feed_series(price=3500)), s, NOW)


def test_price_event_accepts_small_basis(tmp_path):
    s = settings(tmp_path)
    p = cm.price_event(ladder(center=4000), "GC=F", FakeFeed(feed_series(price=4020)), s, NOW)
    assert abs(p.basis) < 0.01
    assert p.sigma > 0


def test_cheap_tail_gets_edge_when_ladder_underprices_volatility(tmp_path):
    """Ladder quoted at 0.5% width; the feed has been moving ~1.5% over the horizon."""
    s = settings(tmp_path)
    markets = ladder(center=4000, sigma=0.005)
    far = {
        "ticker": "KXGOLDD-26OCT19-T4055", "event_ticker": "KXGOLDD-26OCT19",
        "series_ticker": "KXGOLDD", "strike_type": "greater", "floor_strike": 4055.0,
        "cap_strike": None, "yes_bid_dollars": "0.0300", "yes_ask_dollars": "0.0500",
        "volume_fp": "500", "market_type": "binary", "status": "active",
        "close_time": CLOSE.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    pricer = cm.CommodityPricer(FakeFeed(feed_series(price=4000, per_bar=0.0018)))
    priced = pricer.price_markets([*markets, far], s, NOW)
    p, note = priced[far["ticker"]]
    assert p > 0.12  # a 1.4% move in 6h with ~1.5% realized width is not a 5-cent event
    assert "ladder center" in note


class FakeClient:
    def __init__(self, markets):
        self.markets = markets
        self.bought = []

    def fill_event_info(self, tickers):
        return {}

    def candlesticks(self, series, ticker, hours=6):
        return []

    def event_markets(self, event_ticker):
        return [dict(m) for m in self.markets if m["event_ticker"] == event_ticker]

    def orderbook(self, ticker):
        return {"orderbook_fp": {"no_dollars": [["0.5000", "500.00"]]}}

    def buy_yes_ioc(self, ticker, count, price, client_order_id=None):
        self.bought.append((ticker, count))
        return {"fill_count": count, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "o"}


def tail_event():
    markets = ladder(center=4000, sigma=0.005)
    markets.append({
        "ticker": "KXGOLDD-26OCT19-T4055", "event_ticker": "KXGOLDD-26OCT19",
        "series_ticker": "KXGOLDD", "strike_type": "greater", "floor_strike": 4055.0,
        "cap_strike": None, "yes_bid_dollars": "0.0300", "yes_ask_dollars": "0.0500",
        "volume_fp": "500", "market_type": "binary", "status": "active",
        "close_time": CLOSE.strftime("%Y-%m-%dT%H:%M:%SZ"), "category": "Commodities",
    })
    for m in markets:
        m.setdefault("category", "Commodities")
    return markets


def commodity_shots(s, markets, quant):
    """The commodity lane's shortlist, through the same path a scan uses."""
    estimators = build_estimators(None, None, None, quant)
    if not s.commodities_enabled:
        return []
    return runner.lane_shots(s, FakeClient(markets), markets, spec_for(s, "commodity"), estimators, NOW)


def test_commodity_shots_pick_one_per_event_with_edge(tmp_path):
    s = settings(tmp_path)
    quant = cm.CommodityPricer(FakeFeed(feed_series(price=4000, per_bar=0.0018)))
    shots = commodity_shots(s, tail_event(), quant)
    assert len(shots) == 1
    assert shots[0].lane == "commodity"
    assert shots[0].event_ticker == "KXGOLDD-26OCT19"


def test_commodity_shots_empty_when_disabled_or_unpriceable(tmp_path):
    quiet = cm.CommodityPricer(FakeFeed(feed_series(price=4000, per_bar=0.0018)))
    assert commodity_shots(settings(tmp_path, commodities_enabled=False), tail_event(), quiet) == []
    stale = cm.CommodityPricer(FakeFeed(feed_series(age_s=7200)))
    assert commodity_shots(settings(tmp_path), tail_event(), stale) == []
    assert commodity_shots(settings(tmp_path), tail_event(), None) == []


def test_evaluate_commodity_logs_and_uses_no_language_model(tmp_path):
    s = settings(tmp_path)
    store = Store(s.db_path)
    client = FakeClient(tail_event())

    class Boom:
        def estimate(self, *a, **k):
            raise AssertionError("language model must not be called for commodities")

    shot = LongShot(
        ticker="KXGOLDD-26OCT19-T4055", title="gold above 4055", rules="", bid=0.03, ask=0.05,
        volume=500, close_time=CLOSE.strftime("%Y-%m-%dT%H:%M:%SZ"), hours_to_close=6,
        lane="commodity", series="KXGOLDD", event_ticker="KXGOLDD-26OCT19", category="Commodities",
    )
    # evaluate uses the real clock; make the feed fresh against it and close times relative to it.
    real_now = datetime.now(UTC)
    fresh_close = (real_now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for m in client.markets:
        m["close_time"] = fresh_close
    end = real_now.timestamp() - 60
    series = feed_series(price=4000, per_bar=0.0018)
    shift = end - series.last_ts
    quant = cm.CommodityPricer(FakeFeed(cm.Series(
        series.last, series.last_ts + shift, series.closes, [t + shift for t in series.times]
    )))
    out = runner.evaluate(s, client, Boom(), store, shot, None, quant=quant)
    assert out.action == "bet"
    assert out.model_p is not None and out.model_p > 0.1
    row = store.recent_decisions(1)[0]
    assert row["lane"] == "commodity"
    assert "ladder center" in row["context"]
    assert client.bought == []  # observe mode never orders


def test_commodity_decisions_do_not_enter_the_rag_index(tmp_path):
    s = settings(tmp_path)
    store = Store(s.db_path)
    store.add_decision(
        ticker="KXGOLDD-X", title="gold above 4160 close", bid=0.01, ask=0.03, model_p=0.2,
        edge=0.1, action="bet", reason="r", mode="observe", close_time="", lane="commodity",
    )
    store.add_decision(
        ticker="KXOTHER-X", title="gold rally headline", bid=0.01, ask=0.03, model_p=0.2,
        edge=0.1, action="bet", reason="r", mode="observe", close_time="", lane="moonshot",
    )
    store.set_outcome("KXGOLDD-X", "no")
    store.set_outcome("KXOTHER-X", "no")
    assert store.rebuild_rag() == 1
