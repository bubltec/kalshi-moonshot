import json

from kalshi_moonshot import itickfeed


def quote(s="SPX", ld=7520.93, ts=0, kind="quote"):
    return {"code": 1, "data": {"s": s, "r": "GB", "ld": ld, "t": 1794348405049, "ts": ts, "type": kind}}


def test_a_quote_becomes_an_index_event_with_its_source_time():
    e = itickfeed.parse_message(quote(), 1.0)[0]
    assert e["kind"] == "index" and e["index_id"] == "IT_SPX" and e["value"] == 7520.93 and e["src_ms"] == 1794348405049.0


def test_other_symbols_trades_halts_and_bad_prices_are_dropped():
    assert itickfeed.parse_message(quote(s="DJI"), 1.0) == []
    assert itickfeed.parse_message(quote(kind="tick"), 1.0) == []
    assert itickfeed.parse_message(quote(ts=1), 1.0) == []
    assert itickfeed.parse_message(quote(ld=0), 1.0) == []
    assert itickfeed.parse_message({"code": 0, "data": quote()["data"]}, 1.0) == []
    assert itickfeed.parse_message({"code": 1, "resAc": "auth"}, 1.0) == []


def test_one_tick_a_second():
    got = []
    f = itickfeed.ITickFeed(got.append, "k")
    for now in (100.1, 100.6, 101.2):
        f.handle(json.dumps(quote()), now)
    assert len(got) == 2
