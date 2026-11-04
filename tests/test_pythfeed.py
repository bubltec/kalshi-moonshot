import json

from kalshi_moonshot import pythfeed
from kalshi_moonshot.kalshiws import strike_index_for

GOLD = pythfeed.FEEDS["PYTH_GOLD"][1]


def frame(price=390093, expo=-2, fid=GOLD, t=1_800_000_000):
    return json.dumps({"parsed": [{"id": fid, "price": {"price": str(price), "expo": expo, "publish_time": t}}]})


def test_a_pyth_update_becomes_an_index_event_on_the_strike_scale():
    [e] = pythfeed.parse_update(json.loads(frame()), 1_800_000_000.5)
    assert e["kind"] == "index" and e["index_id"] == "PYTH_GOLD" and abs(e["value"] - 3900.93) < 1e-6
    assert e["src_ms"] == 1_800_000_000_000.0


def test_unknown_feeds_and_bad_prices_are_dropped():
    assert pythfeed.parse_update(json.loads(frame(fid="ab" * 32)), 1.0) == []
    assert pythfeed.parse_update(json.loads(frame(price=0)), 1.0) == []
    assert pythfeed.parse_update({"parsed": [{"id": GOLD, "price": {}}]}, 1.0) == []


def test_one_tick_a_second_per_index():
    got = []
    feed = pythfeed.PythFeed(got.append, "k")
    for ts in (10.1, 10.5, 10.9, 11.2):
        feed.handle(frame(), ts)
    assert [int(e["ts"]) for e in got] == [10, 11]


def test_series_find_their_pyth_index_and_crypto_keeps_its_own():
    assert strike_index_for("KXWTI15M") == "PYTH_WTI" and strike_index_for("KXPALLADIUM15M") == "PYTH_PALLADIUM"
    assert strike_index_for("KXBTC15M") == "BRTI" and strike_index_for("KXEURUSD15M") is None


def test_no_key_no_feed(monkeypatch):
    monkeypatch.delenv("PYTH_API_KEY", raising=False)
    monkeypatch.delenv("PYTH_API_KEY_PATH", raising=False)
    assert pythfeed.api_key() == ""


def test_the_treasury_yield_feeds_map_to_their_series_and_parse():
    assert pythfeed.index_for_series("KX30YRRATE15M") == "PYTH_US30Y" and pythfeed.index_for_series("KX2YRRATE15M") == "PYTH_US2Y"
    assert pythfeed.index_for_series("KX10YRRATE15M") == "PYTH_US10Y" and pythfeed.index_for_series("KX5YRRATE15M") == "PYTH_US5Y"
    frame = {"parsed": [{"id": pythfeed.FEEDS["PYTH_US30Y"][1], "price": {"price": "530017", "expo": -5, "publish_time": 1_800_000_000}}]}
    [e] = pythfeed.parse_update(frame, 1_800_000_000.5)
    assert e["index_id"] == "PYTH_US30Y" and abs(e["value"] - 5.30017) < 1e-9
