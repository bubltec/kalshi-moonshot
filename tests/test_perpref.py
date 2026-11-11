from kalshi_moonshot import perpref


def market(ticker="KXGOLDPERP", price="4.0437", size="0.001000", ts=1794400132000):
    return {"ticker": ticker, "contract_size": size, "reference_price": {"price": price, "ts_ms": ts}}


def test_a_reference_becomes_an_index_event_in_the_underlyings_units():
    e = perpref.parse_markets([market()], 5.0)[0]
    assert e["kind"] == "index" and e["index_id"] == "KP_GOLD" and abs(e["value"] - 4043.7) < 1e-6 and e["src_ms"] == 1794400132000.0 and e["ts"] == 5.0
    assert abs(perpref.parse_markets([market("KXDOGEPERP", "6.7853", "100.000000")], 1.0)[0]["value"] - 0.067853) < 1e-9


def test_other_perps_and_unusable_readings_are_dropped():
    assert perpref.parse_markets([market("KXAAPLPERP")], 1.0) == []
    assert perpref.parse_markets([{"ticker": "KXGOLDPERP", "contract_size": "0.001"}], 1.0) == []
    assert perpref.parse_markets([market(price="0")], 1.0) == []
    assert perpref.parse_markets([market(size="0")], 1.0) == []


def test_a_reading_is_recorded_once_per_source_timestamp():
    got = []
    f = perpref.PerpRefFeed(got.append)
    assert f.handle([market(), market("KXSILVERPERP", "5.2848", "0.100000")], 1.0) == 2
    assert f.handle([market(), market("KXSILVERPERP", "5.2850", "0.100000", ts=1794400133000)], 2.0) == 1  # gold's reference did not move on
    assert [e["index_id"] for e in got] == ["KP_GOLD", "KP_SILVER", "KP_SILVER"]
