import json
import time

import pytest

from kalshi_moonshot import commodities as cm
from kalshi_moonshot.krakenfeed import KrakenFeed, RoutingFeed, parse_ticker

UPDATE = {
    "channel": "ticker", "type": "update",
    "data": [{"symbol": "BTC/USD", "last": 81910.5, "bid": 81910.4, "ask": 81910.5,
              "timestamp": "2026-10-19T17:00:44.842180Z"}],
}


def test_parse_ticker_reads_the_v2_shape():
    ((symbol, price, ts),) = parse_ticker(UPDATE)
    assert (symbol, price) == ("BTC/USD", 81910.5)
    assert ts == pytest.approx(1792429244.84, abs=1)


def test_parse_ticker_ignores_everything_else():
    assert parse_ticker({"channel": "heartbeat"}) == []
    assert parse_ticker({"method": "subscribe", "success": True}) == []
    assert parse_ticker({"channel": "ticker", "type": "update", "data": [{"symbol": "X"}]}) == []
    assert parse_ticker({"channel": "ticker", "type": "update", "data": [{"symbol": "X", "last": "n/a", "timestamp": "bad"}]}) == []


class Bars:
    """Stands in for httpx so no test touches the network."""

    def __init__(self, n=200, price=82000.0):
        now = time.time()
        self.rows = [[now - (n - i) * 300, 0, 0, 0, price + (i % 2), 0, 0, 0] for i in range(n)]
        self.calls = 0

    def get(self, url, params=None):
        self.calls += 1
        rows = self.rows
        pair = (params or {}).get("pair", "")

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"error": [], "result": {"XXBTZUSD" if pair == "XBTUSD" else pair: rows, "last": 1}}

        return R()

    def close(self):
        pass


def feed_with_tick(price=82010.0, age=3.0):
    bars = Bars()
    f = KrakenFeed(["kraken:BTC/USD"], http=bars)
    f._thread = type("T", (), {"is_alive": lambda self: True})()  # pretend the socket thread runs
    f.handle(json.dumps({"channel": "ticker", "type": "update", "data": [{
        "symbol": "BTC/USD", "last": price,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime(time.time() - age)),
    }]}))
    return f, bars


def test_series_combines_the_live_tick_with_cached_history():
    f, bars = feed_with_tick(price=82010.0)
    s = f.series("kraken:BTC/USD")
    assert s.last == 82010.0
    assert 0 <= time.time() - s.last_ts < 10
    assert len(s.closes) == 200 and len(s.times) == 200
    f.series("kraken:BTC/USD")
    assert bars.calls == 1  # history is cached for a minute


def test_series_with_no_price_yet_fails_cleanly(monkeypatch):
    monkeypatch.setattr("kalshi_moonshot.krakenfeed.FIRST_TICK_WAIT", 0.05)
    f = KrakenFeed(["kraken:ETH/USD"], http=Bars())
    f._thread = type("T", (), {"is_alive": lambda self: True})()
    with pytest.raises(cm.CommodityError, match="no Kraken price"):
        f.series("kraken:ETH/USD")


def test_a_new_symbol_is_added_to_the_subscription():
    f, _ = feed_with_tick()
    f._subscribe("PAXG/USD")
    assert f._symbols == {"BTC/USD", "PAXG/USD"}


def test_bad_messages_are_dropped_not_raised():
    f, _ = feed_with_tick()
    f.handle("not json")
    f.handle(json.dumps({"channel": "ticker", "type": "update", "data": "oops"}))


def test_routing_sends_kraken_symbols_to_kraken_and_the_rest_elsewhere():
    kraken, _ = feed_with_tick()
    seen = []

    class Other:
        def series(self, symbol):
            seen.append(symbol)
            return "yahoo-series"

    r = RoutingFeed(kraken, Other())
    assert r.series("kraken:BTC/USD").last == 82010.0
    assert r.series("SI=F") == "yahoo-series" and seen == ["SI=F"]


def test_the_default_pricer_routes_through_kraken():
    pricer = cm.CommodityPricer()
    assert isinstance(pricer.feed, RoutingFeed)


def test_the_default_feed_subscribes_to_every_kraken_pair_up_front():
    feed = cm.CommodityPricer().feed
    assert {"BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD"} <= feed.kraken._symbols
    assert {"xyz:GOLD", "xyz:SILVER", "xyz:CL", "xyz:COPPER"} <= feed.hyperliquid._coins


def test_bitcoin_history_is_requested_under_its_rest_name():
    f, bars = feed_with_tick()
    f.series("kraken:BTC/USD")
    assert bars.calls == 1 and f._bars["BTC/USD"][1]  # Bars only answers the XBTUSD pair name
