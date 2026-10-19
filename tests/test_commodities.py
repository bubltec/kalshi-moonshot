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
GOLD = cm.Instrument("KXGOLD", "GC=F", ("D", "H", "W", "15M"))


def settings(tmp_path, **over):
    s = config.load()
    base = {
        "data_dir": tmp_path, "mode": "observe", "env": "demo", "skip_categories": frozenset(),
        "commodities_enabled": True, "commodity_min_edge": 0.03, "commodity_max_basis": 0.04,
        "commodity_vol_mult": 1.25, "commodity_min_volume": 0.0, "commodity_min_price": 0.70,
        "commodity_max_price": 0.97, "commodity_max_spread": 0.04, "commodity_tail_floor": 0.01,
        "commodity_basis_unc": 0.0015, "commodity_basis_age_hours": 6.0,
        "commodity_max_stale_minutes": 20.0, "commodity_min_hours_to_close": 0.03,
        "commodity_max_hours_to_close": 24.0, "max_usd_per_trade": 5.0,
        "commodity_usd_per_trade": 2.0, "min_order_usd": 0.0,
    }
    return type(s)(**{**s.__dict__, **base, **over})


def ladder(center=4000.0, sigma=0.005, strikes=range(3900, 4101, 10), hours=0.5, series="KXGOLDD",
           now=NOW):
    """An event's 'above K' markets quoted from a lognormal with the given width."""
    close = (now + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = []
    for k in strikes:
        p = cm.prob_above(center, k, sigma)
        bid, ask = max(p - 0.01, 0.01), min(p + 0.01, 0.99)
        out.append({
            "ticker": f"{series}-26OCT19-T{k}", "event_ticker": f"{series}-26OCT19",
            "series_ticker": series, "strike_type": "greater", "floor_strike": float(k),
            "cap_strike": None, "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}",
            "volume_fp": "500", "market_type": "binary", "status": "active",
            "close_time": close, "category": "Commodities",
        })
    return out


def feed_series(price=4000.0, per_bar=0.0005, bars=600, age_s=60.0, now=NOW):
    """Alternating +/- moves of exactly per_bar, so realized per-bar sigma is per_bar."""
    end = now.timestamp() - age_s
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


def test_symbol_mapping_covers_short_series_and_crypto_only():
    assert cm.symbol_for("KXGOLDD") == "GC=F"
    assert cm.symbol_for("KXGOLD15M") == "GC=F"
    assert cm.symbol_for("KXWTIH") == "CL=F"
    assert cm.symbol_for("KXBTC15M") == "BTC-USD"
    assert cm.symbol_for("KXETHD") == "ETH-USD"
    assert cm.symbol_for("KXGOLDMON") is None  # monthly: too long for realized vol
    assert cm.symbol_for("KXBTCMAXD") is None
    assert cm.symbol_for("KXNBAGAME") is None


def test_prob_above_is_half_at_center_and_monotone():
    assert cm.prob_above(100, 100, 0.02) == pytest.approx(0.5)
    assert cm.prob_above(100, 98, 0.02) > cm.prob_above(100, 100, 0.02) > cm.prob_above(100, 102, 0.02)
    assert cm.prob_above(100, 100, 0.0) == 0.0
    assert cm.prob_above(100, 99, 0.0) == 1.0


def test_fit_recovers_center_and_width():
    fit = cm.fit_ladder(ladder(center=4000, sigma=0.012, strikes=range(3900, 4101, 10)))
    assert fit is not None
    assert fit.center == pytest.approx(4000, rel=0.002)
    assert fit.sigma == pytest.approx(0.012, rel=0.1)
    assert fit.r2 > 0.99


def test_fit_needs_enough_liquid_strikes():
    assert cm.fit_ladder(ladder(strikes=[3990, 4000, 4010])) is None
    wide = ladder()
    for m in wide:
        m["yes_bid_dollars"] = "0.0000"  # no bids anywhere
    assert cm.fit_ladder(wide) is None


def test_market_prob_shapes():
    c, sig = 100.0, 0.02
    above = {"strike_type": "greater", "floor_strike": 101, "cap_strike": None}
    at_least = {"strike_type": "greater_or_equal", "floor_strike": 101, "cap_strike": None}
    below = {"strike_type": "less", "floor_strike": None, "cap_strike": 101}
    between = {"strike_type": "between", "floor_strike": 99, "cap_strike": 101}
    assert cm.market_prob(above, c, sig) + cm.market_prob(below, c, sig) == pytest.approx(1.0)
    assert cm.market_prob(at_least, c, sig) == cm.market_prob(above, c, sig)
    assert 0 < cm.market_prob(between, c, sig) < 0.5
    assert cm.market_prob({"strike_type": "structured"}, c, sig) is None


def test_realized_sigma_scales_with_sqrt_time():
    series = feed_series(per_bar=0.0005)
    one, four = cm.realized_sigma(series, 1.0), cm.realized_sigma(series, 4.0)
    assert four == pytest.approx(2 * one, rel=1e-6)
    bars = cm.OPEN_FRACTION * 3600 / cm.BAR_SECONDS
    assert one == pytest.approx(0.0005 * math.sqrt(bars), rel=0.01)


def test_realized_sigma_ignores_session_gaps():
    series = feed_series()
    times = list(series.times)
    for i in range(300, len(times)):
        times[i] += 3 * 86400  # a weekend in the middle
    gapped = cm.Series(series.last, times[-1], series.closes, times)
    assert cm.realized_sigma(gapped, 2.0) == pytest.approx(cm.realized_sigma(series, 2.0), rel=0.01)


def test_price_event_rejects_a_stale_feed(tmp_path):
    with pytest.raises(cm.CommodityError, match="old"):
        cm.price_event(ladder(), GOLD, FakeFeed(feed_series(age_s=3600)), settings(tmp_path), NOW)


def test_price_event_rejects_the_wrong_contract(tmp_path):
    with pytest.raises(cm.CommodityError, match="different contract"):
        cm.price_event(ladder(center=4000), GOLD, FakeFeed(feed_series(price=3500)), settings(tmp_path), NOW)


def test_an_old_feed_widens_the_price_instead_of_being_trusted(tmp_path):
    s = settings(tmp_path)
    fresh = cm.price_event(ladder(), GOLD, FakeFeed(feed_series(age_s=30)), s, NOW)
    stale = cm.price_event(ladder(), GOLD, FakeFeed(feed_series(age_s=600)), s, NOW)
    # variance grows with the time left plus the feed's age: (0.5h + 10min) vs (0.5h + 30s)
    assert stale.sigma / fresh.sigma == pytest.approx(math.sqrt((0.5 + 600 / 3600) / (0.5 + 30 / 3600)), rel=0.02)


def test_a_thin_ladder_reuses_the_remembered_basis_with_extra_width(tmp_path):
    s = settings(tmp_path)
    memory: dict = {}
    feed = FakeFeed(feed_series(price=4020))  # feed sits 0.5% above the ladder: a basis
    good = cm.price_event(ladder(center=4000), GOLD, feed, s, NOW, memory=memory)
    assert not good.remembered and abs(good.basis + 0.005) < 0.001
    thin = cm.price_event(ladder(strikes=[3990, 4000]), GOLD, feed, s, NOW, memory=memory)
    assert thin.remembered
    assert thin.center == pytest.approx(good.center, rel=0.001)
    assert thin.sigma > good.sigma  # not knowing the basis exactly costs width
    with pytest.raises(cm.CommodityError, match="no recent basis"):
        cm.price_event(ladder(strikes=[3990, 4000]), GOLD, feed, s, NOW, memory={})
    old = {"KXGOLD": (-0.005, NOW.timestamp() - 7 * 3600, "KXGOLDD")}
    with pytest.raises(cm.CommodityError, match="no recent basis"):
        cm.price_event(ladder(strikes=[3990, 4000]), GOLD, feed, s, NOW, memory=old)


def pricer(price=4000.0, per_bar=0.0005, age_s=60.0):
    return cm.CommodityPricer(FakeFeed(feed_series(price=price, per_bar=per_bar, age_s=age_s)))


def test_decided_strikes_price_near_certain_on_both_sides(tmp_path):
    """Half an hour left, realized width ~0.15%: a strike 3% of the width away is decided."""
    s = settings(tmp_path)
    priced = pricer().price_markets(ladder(), s, NOW)
    below_spot = priced["KXGOLDD-26OCT19-T3970"][0]  # price must fall 0.75% to miss: ~5 widths
    above_spot = priced["KXGOLDD-26OCT19-T4030"][0]
    assert below_spot == pytest.approx(0.99)  # clamped by the tail floor (model risk)
    assert above_spot == pytest.approx(0.01)
    near = priced["KXGOLDD-26OCT19-T4000"][0]
    assert 0.3 < near < 0.7  # the coin-flip strike is not claimed as certain


def side_shots(s, markets, quant):
    estimators = build_estimators(None, None, None, quant)
    return runner.lane_shots(s, FakeClient(markets), markets, spec_for(s, "commodity"), estimators, NOW)


def test_lane_buys_the_decided_side_whichever_it_is(tmp_path):
    s = settings(tmp_path)
    markets = ladder()
    for m in markets:  # make the decided strikes cheap enough to leave an edge
        k = m["floor_strike"]
        if k == 3970:
            m["yes_bid_dollars"], m["yes_ask_dollars"] = "0.9000", "0.9300"
        if k == 4030:
            m["yes_bid_dollars"], m["yes_ask_dollars"] = "0.0500", "0.0800"
    spec = spec_for(s, "commodity")
    assert spec.sides == ("yes", "no")
    cands = runner.lane_candidates(markets, spec, s, NOW)
    by = {(x.ticker, x.side): x for x in cands}
    yes = by[("KXGOLDD-26OCT19-T3970", "yes")]
    no = by[("KXGOLDD-26OCT19-T4030", "no")]
    assert yes.ask == pytest.approx(0.93) and yes.bid == pytest.approx(0.90)
    assert no.ask == pytest.approx(0.95)  # pay 1 - YES bid
    assert no.bid == pytest.approx(0.92)  # could sell back at 1 - YES ask
    shots = side_shots(s, markets, pricer())
    assert len(shots) == 1  # one side per event: the better edge


def test_a_longshot_is_not_a_candidate_for_this_lane(tmp_path):
    s = settings(tmp_path)
    spec = spec_for(s, "commodity")
    cands = runner.lane_candidates(ladder(), spec, s, NOW)
    assert all(0.70 <= x.ask <= 0.97 for x in cands)
    assert all(x.ask >= 0.70 for x in cands if x.side == "no")


class FakeClient:
    def __init__(self, markets):
        self.markets = markets
        self.bought = []
        self.bought_no = []

    def fill_event_info(self, tickers):
        return {}

    def candlesticks(self, series, ticker, hours=6):
        return []

    def event_markets(self, event_ticker):
        return [dict(m) for m in self.markets if m["event_ticker"] == event_ticker]

    def orderbook(self, ticker):
        return {"orderbook_fp": {"no_dollars": [["0.0500", "500.00"]], "yes_dollars": [["0.0500", "500.00"]]}}

    def buy_yes_ioc(self, ticker, count, price, client_order_id=None):
        self.bought.append((ticker, count))
        return {"fill_count": count, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "y"}

    def buy_no_ioc(self, ticker, count, price, client_order_id=None):
        self.bought_no.append((ticker, count, price))
        return {"fill_count": count, "average_fill_price": price, "average_fee_paid": 0.0, "order_id": "n"}


def decided_event(now):
    markets = ladder(now=now, hours=0.5)
    for m in markets:
        k = m["floor_strike"]
        if k == 3970:
            m["yes_bid_dollars"], m["yes_ask_dollars"] = "0.9000", "0.9300"
        if k == 4030:
            m["yes_bid_dollars"], m["yes_ask_dollars"] = "0.0500", "0.0800"
    return markets


def live_pricer():
    now = datetime.now(UTC)
    return cm.CommodityPricer(FakeFeed(feed_series(price=4000.0, per_bar=0.0005, age_s=30, now=now)))


def shot_for(markets, ticker, side):
    now = datetime.now(UTC)
    s = settings(None or __import__("pathlib").Path("/nonexistent"))
    spec = spec_for(s, "commodity")
    return next(x for x in runner.lane_candidates(markets, spec, s, now) if (x.ticker, x.side) == (ticker, side))


@pytest.mark.parametrize(("ticker", "side"), [("KXGOLDD-26OCT19-T3970", "yes"), ("KXGOLDD-26OCT19-T4030", "no")])
def test_evaluate_logs_the_side_in_its_own_frame_and_never_calls_the_language_model(tmp_path, ticker, side):
    s = settings(tmp_path)
    markets = decided_event(datetime.now(UTC))
    store = Store(s.db_path)
    client = FakeClient(markets)

    class Boom:
        def estimate(self, *a, **k):
            raise AssertionError("language model must not be called for commodities")

    shot = next(
        x for x in runner.lane_candidates(markets, spec_for(s, "commodity"), s, datetime.now(UTC))
        if (x.ticker, x.side) == (ticker, side)
    )
    out = runner.evaluate(s, client, Boom(), store, shot, None, quant=live_pricer())
    assert out.action == "bet"
    assert out.model_p == pytest.approx(0.99)  # chance the contract we hold pays
    row = store.recent_decisions(1)[0]
    assert row["side"] == side and row["lane"] == "commodity"
    assert "center" in row["context"]
    assert client.bought == [] and client.bought_no == []  # observe mode never orders


def test_trade_mode_buys_no_with_the_no_order_and_records_the_contract(tmp_path):
    s = settings(tmp_path, mode="trade", env="demo")
    markets = decided_event(datetime.now(UTC))
    store = Store(s.db_path)
    client = FakeClient(markets)
    shot = next(
        x for x in runner.lane_candidates(markets, spec_for(s, "commodity"), s, datetime.now(UTC))
        if (x.ticker, x.side) == ("KXGOLDD-26OCT19-T4030", "no")
    )
    runner.evaluate(s, client, None, store, shot, None, quant=live_pricer())
    assert client.bought == [] and len(client.bought_no) == 1
    assert client.bought_no[0][2] == pytest.approx(0.95)
    order = store.db.execute("SELECT contract, side FROM orders").fetchone()
    assert (order["contract"], order["side"]) == ("no", "buy")


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


def test_longshot_dataclass_default_side_is_yes():
    assert LongShot("T", "t", "", 0.1, 0.2, 1.0, "", 1.0).side == "yes"
