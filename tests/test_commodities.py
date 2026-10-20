import math
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_moonshot import commodities as cm
from kalshi_moonshot import config, lanes, runner
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
        "commodity_max_price": 0.97, "commodity_min_return": 0.10, "commodity_max_spread": 0.04, "commodity_tail_floor": 0.01,
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


def test_symbol_mapping_covers_short_series_commodities_and_crypto_only():
    assert cm.symbol_for("KXGOLDD") == "hl:xyz:GOLD"
    assert cm.symbol_for("KXGOLD15M") == "hl:xyz:GOLD"
    assert cm.symbol_for("KXSILVER15M") == "hl:xyz:SILVER"
    assert cm.symbol_for("KXWTI15M") == "hl:xyz:CL"
    assert cm.symbol_for("KXWTI") == "hl:xyz:CL"  # daily WTI has no suffix
    assert cm.symbol_for("KXCOPPER15M") == "hl:xyz:COPPER"
    assert cm.symbol_for("KXNATGAS15M") == "hl:xyz:NATGAS"
    assert cm.symbol_for("KXBRENT15M") is None  # Kalshi has no 15-minute Brent
    assert cm.symbol_for("KXBTC15M") == "kraken:BTC/USD"
    assert cm.symbol_for("KXETHD") == "kraken:ETH/USD"
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
    old = {"KXGOLDD": (-0.005, NOW.timestamp() - 7 * 3600, 0.0)}
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
            m["yes_bid_dollars"], m["yes_ask_dollars"] = "0.8500", "0.8700"
        if k == 4030:
            m["yes_bid_dollars"], m["yes_ask_dollars"] = "0.1200", "0.1500"
    spec = spec_for(s, "commodity")
    assert spec.sides == ("yes", "no")
    cands = runner.lane_candidates(markets, spec, s, NOW)
    by = {(x.ticker, x.side): x for x in cands}
    yes = by[("KXGOLDD-26OCT19-T3970", "yes")]
    no = by[("KXGOLDD-26OCT19-T4030", "no")]
    assert yes.ask == pytest.approx(0.87) and yes.bid == pytest.approx(0.85)
    assert no.ask == pytest.approx(0.88)  # pay 1 - YES bid
    assert no.bid == pytest.approx(0.85)  # could sell back at 1 - YES ask
    shots = side_shots(s, markets, pricer())
    assert len(shots) == 1  # one side per event: the better edge


def test_a_longshot_is_not_a_candidate_for_this_lane(tmp_path):
    s = settings(tmp_path)
    spec = spec_for(s, "commodity")
    cands = runner.lane_candidates(ladder(), spec, s, NOW)
    assert all(0.70 <= x.ask <= 0.97 for x in cands)
    assert all(x.ask >= 0.70 for x in cands if x.side == "no")


def test_a_win_must_pay_at_least_ten_percent_after_the_fee(tmp_path):
    """A 96-cent entry makes about 4% if it wins: not worth risking the whole stake."""
    from kalshi_moonshot import strategy

    s = settings(tmp_path, commodity_min_return=0.10, commodity_max_price=0.98)
    spec = spec_for(s, "commodity")
    assert spec.min_return == 0.10
    base = ladder()[3]
    def reason(ask, bid):
        m = dict(base, yes_ask_dollars=f"{ask:.4f}", yes_bid_dollars=f"{bid:.4f}", volume_fp="500")
        return strategy.lane_reject_reason(m, spec, s, NOW)
    assert reason(0.88, 0.87) is None  # +12.7% if it wins
    assert reason(0.90, 0.89) is None  # +10.3%
    assert "under 10%" in reason(0.92, 0.91)  # +8.1%
    assert "under 10%" in reason(0.96, 0.95)  # +3.9%
    off = settings(tmp_path, commodity_min_return=0.0)
    assert strategy.lane_reject_reason(
        dict(base, yes_ask_dollars="0.9600", yes_bid_dollars="0.9500"), spec_for(off, "commodity"), off, NOW
    ) is None


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


def decided_event(now, hours=0.5):
    markets = ladder(now=now, hours=hours)
    for m in markets:
        k = m["floor_strike"]
        if k == 3970:
            m["yes_bid_dollars"], m["yes_ask_dollars"] = "0.8500", "0.8700"
        if k == 4030:
            m["yes_bid_dollars"], m["yes_ask_dollars"] = "0.1200", "0.1500"
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
    assert client.bought_no[0][2] == pytest.approx(0.88)
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


def test_a_basis_learned_from_an_old_feed_costs_extra_width(tmp_path):
    """A 10-minute-old feed makes the measured basis include an unseen move; reuse must not hide it."""
    s = settings(tmp_path)
    feed = FakeFeed(feed_series(price=4020))
    fresh = {"KXGOLDD": (-0.005, NOW.timestamp(), 0.0)}
    delayed = {"KXGOLDD": (-0.005, NOW.timestamp(), 600 / 3600)}
    thin = ladder(strikes=[3990, 4000])
    a = cm.price_event(thin, GOLD, feed, s, NOW, memory=fresh)
    b = cm.price_event(thin, GOLD, feed, s, NOW, memory=delayed)
    assert a.remembered and b.remembered and b.sigma > a.sigma


BTC = cm.Instrument("KXBTC", "BTC-USD", ("", "D", "15M"), realtime=True)


def up_down(strike, series="KXBTC15M", minutes=3.0, bid=0.05, ask=0.06, now=NOW):
    """A 15-minute 'price up?' market: one contract, strike = the price at the start of the window."""
    close = (now + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [{
        "ticker": f"{series}-26OCT191115-15", "event_ticker": f"{series}-26OCT191115",
        "series_ticker": series, "strike_type": "greater_or_equal", "floor_strike": float(strike),
        "cap_strike": None, "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}",
        "volume_fp": "5000", "market_type": "binary", "status": "active", "close_time": close,
        "category": "Crypto",
    }]


def test_a_15_minute_crypto_market_is_priced_from_the_real_time_price(tmp_path):
    s = settings(tmp_path)
    feed = FakeFeed(feed_series(price=82000.0, per_bar=0.0004, age_s=8))
    below = cm.price_event(up_down(strike=83000), BTC, feed, s, NOW, memory={})  # 1.2% above the price
    assert below.anchor == "spot" and below.basis == 0.0
    p_up = cm.market_prob(up_down(strike=83000)[0], below.center, below.sigma)
    assert p_up < 0.02  # three minutes cannot cover a 1.2% climb
    p_down = cm.market_prob(up_down(strike=81000)[0], below.center, below.sigma)
    assert p_down > 0.98  # and the price is already well above this start level


def test_a_futures_basis_never_carries_across_series(tmp_path):
    """Daily WTI settles on a different price than the hourly or 15-minute contracts."""
    s = settings(tmp_path)
    wti = cm.Instrument("KXWTI", "CL=F", ("", "H", "W"))
    feed = FakeFeed(feed_series(price=4000.0))
    memory = {"KXWTI": (-0.029, NOW.timestamp(), 0.0)}  # learned on the daily ladder
    thin = ladder(strikes=[3990, 4000], series="KXWTIH")
    with pytest.raises(cm.CommodityError, match="no recent basis"):
        cm.price_event(thin, wti, feed, s, NOW, memory=memory)


def test_crypto_is_anchored_to_the_real_time_price_not_to_a_ladder_offset(tmp_path):
    s = settings(tmp_path)
    feed = FakeFeed(feed_series(price=82000.0, per_bar=0.0004, age_s=8))
    memory: dict = {}
    full = ladder(center=82500.0, sigma=0.01, strikes=range(81000, 84001, 250), series="KXBTCD")
    cm.price_event(full, BTC, feed, s, NOW, memory=memory)
    assert "KXBTCD" in memory and "family:KXBTC" not in memory  # only proxies share an offset
    thin = cm.price_event(up_down(82010, series="KXBTC15M"), BTC, feed, s, NOW, memory=memory)
    assert thin.anchor == "spot" and thin.basis == 0.0  # the ladder's 0.6% opinion is not borrowed


SILVER = cm.Instrument("KXSILVER", "hl:xyz:SILVER", ("D", "H", "W", "15M"), realtime=True, proxy=True)


def settled_window(strike, expiration, close, series="KXSILVER15M"):
    return {"ticker": f"{series}-X", "close_time": close.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "floor_strike": strike, "expiration_value": expiration, "strike_type": "greater_or_equal"}


def boundary_world(offset=-0.0003, noise=0.0, n=10, now=NOW, price=57.0):
    """n settled 15-minute windows whose reference sits `offset` (ln) from the feed's candle close."""
    closes_at = {}
    markets = []
    t0 = int(now.timestamp()) // 900 * 900 - n * 900
    px = price
    for i in range(n):
        o, c = t0 + i * 900, t0 + (i + 1) * 900
        closes_at[o - 60], closes_at[c - 60] = px, px * 1.0004
        ref_o = px * math.exp(offset + (noise if i % 2 else -noise))
        ref_c = px * 1.0004 * math.exp(offset + (noise if i % 2 else -noise))
        markets.append(settled_window(round(ref_o, 4), str(round(ref_c, 4)), datetime.fromtimestamp(c, UTC)))
        px *= 1.0004
    return markets, closes_at


def test_the_offset_to_kalshis_reference_is_read_off_settled_windows():
    markets, minutes = boundary_world(offset=-0.0003, n=10)
    refs = cm.boundary_refs(markets)
    assert len(refs) >= 10
    wb = cm.window_basis(refs, minutes, NOW.timestamp())
    assert wb is not None and wb.basis == pytest.approx(-0.0003, abs=2e-5) and wb.sd < 1e-4


def test_too_few_windows_teach_nothing():
    markets, minutes = boundary_world(n=2)
    assert cm.window_basis(cm.boundary_refs(markets), minutes, NOW.timestamp()) is None


def test_a_constant_large_offset_is_learned_like_any_other():
    """Copper's Kalshi reference sits 1.28% from the feed, and steadily so."""
    markets, minutes = boundary_world(offset=0.0128, n=10, price=6.5)
    wb = cm.window_basis(cm.boundary_refs(markets), minutes, NOW.timestamp())
    assert wb.basis == pytest.approx(0.0128, abs=2e-5)


class WindowFeed(FakeFeed):
    def __init__(self, series, minutes):
        super().__init__(series)
        self.minutes = minutes

    def minute_closes(self, symbol, hours=4.0):
        return self.minutes


class SettledClient:
    def __init__(self, markets):
        self.markets = markets
        self.calls = 0

    def settled_markets(self, series, limit=40):
        self.calls += 1
        return self.markets if series == "KXSILVER15M" else []


def test_the_pricer_learns_each_15_minute_series_and_refreshes_it_sparingly(tmp_path):
    s = settings(tmp_path)
    markets, minutes = boundary_world(offset=-0.0003)
    pricer = cm.CommodityPricer(WindowFeed(feed_series(price=57.0, age_s=3), minutes))
    client = SettledClient(markets)
    assert pricer.learn_windows(client, s, NOW) == 1
    assert "KXSILVER15M" in pricer.windows and pricer.windows["KXSILVER15M"].n >= 6
    calls = client.calls
    assert pricer.learn_windows(client, s, NOW + timedelta(minutes=2)) == 0  # fresh enough
    assert client.calls == calls


def test_a_15_minute_market_is_centered_on_the_feed_plus_its_learned_offset(tmp_path):
    s = settings(tmp_path)
    feed = FakeFeed(feed_series(price=57.0, per_bar=0.0004, age_s=3))
    windows = {"KXSILVER15M": cm.WindowBasis(-0.0003, 0.00012, 12, NOW.timestamp())}
    p = cm.price_event(up_down(57.0, series="KXSILVER15M", minutes=5.0), SILVER, feed, s, NOW,
                       memory={}, windows=windows)
    assert p.anchor == "window" and p.points == 12
    assert p.center == pytest.approx(feed.series("x").last * math.exp(-0.0003))
    assert p.sigma > 0 and "settled windows" in p.note(0.5, 0.1)


def test_without_a_learned_offset_a_proxy_15_minute_series_is_not_guessed(tmp_path):
    s = settings(tmp_path)
    feed = FakeFeed(feed_series(price=57.0, age_s=3))
    with pytest.raises(cm.CommodityError, match="no recent basis"):
        cm.price_event(up_down(57.0, series="KXSILVER15M", minutes=5.0), SILVER, feed, s, NOW,
                       memory={}, windows={})


def test_a_stale_learned_offset_is_dropped(tmp_path):
    s = settings(tmp_path)
    feed = FakeFeed(feed_series(price=57.0, age_s=3))
    old = {"KXSILVER15M": cm.WindowBasis(-0.0003, 0.0001, 12, NOW.timestamp() - 4 * 3600)}
    with pytest.raises(cm.CommodityError):
        cm.price_event(up_down(57.0, series="KXSILVER15M", minutes=5.0), SILVER, feed, s, NOW,
                       memory={}, windows=old)


def test_scoping_refreshes_the_offsets_before_it_lists_windows(tmp_path):
    now = datetime.now(UTC)
    s = settings(tmp_path)
    markets, minutes = boundary_world(now=now)
    quant = cm.CommodityPricer(WindowFeed(feed_series(price=57.0, age_s=3, now=now), minutes))

    class Client(SettledClient):
        def event_info(self):
            return {}

        def series_markets(self, series):
            return []

    store = Store(s.db_path)
    runner.scope_lane(s, Client(markets), store, lanes.spec_for(s, "commodity"), now, quant=quant)
    assert "KXSILVER15M" in quant.windows


def test_the_cooldown_shrinks_as_a_short_market_nears_its_close(tmp_path):
    """Six minutes left: the 15-minute cooldown becomes about a minute, so late moves are seen."""
    import time

    from kalshi_moonshot import lanes
    from kalshi_moonshot.estimators import build_estimators

    now = datetime.now(UTC)
    s = settings(tmp_path)
    store = Store(s.db_path)
    spec = lanes.spec_for(s, "commodity")
    markets = decided_event(now, hours=0.1)
    client = FakeClient(markets)
    store.set_watch("commodity", {"KXGOLDD-26OCT19": {
        "close": markets[0]["close_time"], "category": "Commodities", "series": "KXGOLDD"}})
    est = build_estimators(None, None, None, cm.CommodityPricer(FakeFeed(feed_series(now=now, age_s=30))))
    first = runner.review_lane(s, client, store, spec, est)
    assert len(first) == 1
    ticker = first[0].ticker

    def decide(seconds_ago):
        store.db.execute("DELETE FROM decisions")
        store.add_decision(ticker=ticker, title="t", bid=0.1, ask=0.9, model_p=0.99, edge=0.05,
                           action="pass", mode="observe", ts=time.time() - seconds_ago)

    decide(30)
    assert runner.review_lane(s, client, store, spec, est) == []  # inside ~72s: still cooling
    decide(120)
    assert [x.ticker for x in runner.review_lane(s, client, store, spec, est)] == [ticker]


def test_a_real_time_feed_that_has_gone_quiet_is_refused_not_widened(tmp_path):
    s = settings(tmp_path)
    quiet = FakeFeed(feed_series(price=82000.0, per_bar=0.0004, age_s=300))  # five minutes, crypto
    with pytest.raises(cm.CommodityError, match="old"):
        cm.price_event(up_down(82010), BTC, quiet, s, NOW, memory={})
    futures = FakeFeed(feed_series(price=4000.0, age_s=600))  # ten minutes is normal for futures
    assert cm.price_event(ladder(), GOLD, futures, s, NOW).age_s == 600
