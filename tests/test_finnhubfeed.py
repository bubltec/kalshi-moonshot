import json

from kalshi_moonshot import finnhubfeed


def trades(*rows):
    return json.dumps({"type": "trade", "data": [{"s": s, "p": p, "t": t, "v": 0} for s, p, t in rows]})


def test_a_trade_becomes_an_index_event_with_its_source_time():
    [e] = finnhubfeed.parse_message(json.loads(trades(("OANDA:XAU_USD", 3898.5, 1_800_000_000_250))), 1_800_000_000.4)
    assert e["kind"] == "index" and e["index_id"] == "FH_GOLD" and e["value"] == 3898.5 and e["src_ms"] == 1_800_000_000_250.0


def test_pings_unknown_symbols_and_bad_prices_are_dropped():
    assert finnhubfeed.parse_message({"type": "ping"}, 1.0) == []
    assert finnhubfeed.parse_message(json.loads(trades(("AAPL", 1.0, 1), ("OANDA:XAU_USD", 0, 1))), 1.0) == []


def test_one_tick_a_second_per_index():
    got = []
    feed = finnhubfeed.FinnhubFeed(got.append, "k")
    for ts in (10.1, 10.5, 10.9, 11.2):
        feed.handle(trades(("OANDA:XAU_USD", 3900.0, 1)), ts)
    feed.handle(trades(("OANDA:WTICO_USD", 99.0, 1)), 10.2)
    assert [(e["index_id"], int(e["ts"])) for e in got] == [("FH_GOLD", 10), ("FH_GOLD", 11), ("FH_WTI", 10)]


def test_no_key_no_feed(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("FINNHUB_API_KEY_PATH", raising=False)
    assert finnhubfeed.api_key() == ""
