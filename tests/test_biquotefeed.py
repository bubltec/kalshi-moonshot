import json

from kalshi_moonshot import biquotefeed

RS = biquotefeed.RS


def tick(symbol="XAUUSD", mid=3901.2, ts="2026-11-02T12:59:53.250Z"):
    return json.dumps({"type": 1, "target": "ReceiveTick", "arguments": [{"symbol": symbol, "mid": mid, "bid": mid - 0.1, "ask": mid + 0.1, "timestamp": ts}]}) + RS


def test_a_tick_becomes_an_index_event_with_its_source_time():
    e = biquotefeed.parse_tick(json.loads(tick().rstrip(RS))["arguments"][0], 1_800_000_000.5)
    assert e["kind"] == "index" and e["index_id"] == "BQ_GOLD" and e["value"] == 3901.2
    assert abs(e["src_ms"] - 1_793_624_393_250.0) < 1.0


def test_unknown_symbols_and_bad_prices_are_dropped():
    assert biquotefeed.parse_tick({"symbol": "EURUSD", "mid": 1.1}, 1.0) is None
    assert biquotefeed.parse_tick({"symbol": "XAUUSD", "mid": 0}, 1.0) is None
    assert biquotefeed.parse_tick({"symbol": "XAUUSD"}, 1.0) is None


def test_one_tick_a_second_per_index_and_pings_are_ignored():
    got = []
    feed = biquotefeed.BiQuoteFeed(got.append)
    for ts in (10.1, 10.5, 10.9, 11.2):
        feed.handle(tick() + json.dumps({"type": 6}) + RS, ts)
    feed.handle(tick("XAGUSD", 50.1), 10.2)
    assert [(e["index_id"], int(e["ts"])) for e in got] == [("BQ_GOLD", 10), ("BQ_GOLD", 11), ("BQ_SILVER", 10)]


def test_two_messages_in_one_frame_both_count():
    got = []
    feed = biquotefeed.BiQuoteFeed(got.append)
    feed.handle(tick() + tick("CLUSDT", 99.5), 5.0)
    assert {e["index_id"] for e in got} == {"BQ_GOLD", "BQ_WTI"}


def test_it_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("BIQUOTE_FEED", "0")
    assert not biquotefeed.enabled()
    monkeypatch.delenv("BIQUOTE_FEED")
    assert biquotefeed.enabled()
