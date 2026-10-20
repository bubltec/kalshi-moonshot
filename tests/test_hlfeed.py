import json
import time

import pytest

from kalshi_moonshot import commodities as cm
from kalshi_moonshot.hlfeed import HyperliquidFeed, parse_mids

MSG = {"channel": "allMids", "data": {"mids": {"xyz:GOLD": "4140.75", "xyz:CL": "98.385", "xyz:BAD": "n/a"}}}


def test_parse_mids_reads_the_all_mids_message():
    assert parse_mids(MSG) == {"xyz:GOLD": 4140.75, "xyz:CL": 98.385}
    assert parse_mids({"channel": "subscriptionResponse"}) == {}
    assert parse_mids({"channel": "allMids", "data": None}) == {}


class Candles:
    """Stands in for httpx.Client.post so nothing touches the network."""

    def __init__(self, interval_rows):
        self.rows = interval_rows
        self.calls = []

    def post(self, url, json=None):
        self.calls.append(json["req"]["interval"])
        rows = self.rows

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return rows

        return R()

    def close(self):
        pass


def rows(n=100, start=None, step=300):
    start = start or int(time.time()) - n * step
    return [{"t": (start + i * step) * 1000, "c": str(98.0 + (i % 3) * 0.01)} for i in range(n)]


def live_feed(price=98.4, age=0.0):
    http = Candles(rows())
    f = HyperliquidFeed(["hl:xyz:CL"], http=http)
    f._thread = type("T", (), {"is_alive": lambda self: True})()
    f.handle(json.dumps({"channel": "allMids", "data": {"mids": {"xyz:CL": str(price), "xyz:GOLD": "4100"}}}))
    return f, http


def test_series_combines_the_live_mid_with_cached_5_minute_history():
    f, http = live_feed()
    s = f.series("hl:xyz:CL")
    assert s.last == 98.4 and time.time() - s.last_ts < 5
    assert len(s.closes) == 100 and len(s.times) == 100
    f.series("hl:xyz:CL")
    assert http.calls == ["5m"]  # history is cached for a minute


def test_only_subscribed_coins_are_kept():
    f, _ = live_feed()
    assert "xyz:GOLD" not in f._ticks  # not one we asked for


def test_minute_closes_are_keyed_by_open_time():
    f, _ = live_feed()
    f._http = Candles(rows(n=30, step=60))
    out = f.minute_closes("hl:xyz:CL")
    assert len(out) == 30 and all(isinstance(k, int) for k in out)
    assert f._http.calls == ["1m"]


def test_no_price_yet_fails_cleanly(monkeypatch):
    monkeypatch.setattr("kalshi_moonshot.hlfeed.FIRST_TICK_WAIT", 0.05)
    f = HyperliquidFeed(["hl:xyz:CL"], http=Candles(rows()))
    f._thread = type("T", (), {"is_alive": lambda self: True})()
    with pytest.raises(cm.CommodityError, match="no Hyperliquid price"):
        f.series("hl:xyz:CL")


def test_routing_sends_hl_symbols_to_hyperliquid():
    from kalshi_moonshot.krakenfeed import KrakenFeed, RoutingFeed

    hl, _ = live_feed()
    r = RoutingFeed(KrakenFeed(), hyperliquid=hl)
    assert r.series("hl:xyz:CL").last == 98.4
    assert r.minute_closes.__name__ == "minute_closes"
