from types import SimpleNamespace

from kalshi_moonshot import kalshiws, tape, ticks


def life(event="metadata_updated", **body):
    return {"type": "market_lifecycle_v2", "sid": 13, "seq": 5, "sending_ts_ms": 1794354316000, "msg": {"event_type": event, "market_ticker": "KXSILVER15M-26NOV101900-00", **body}}


def test_a_target_and_a_result_are_parsed():
    e = kalshiws.parse_message(life(strike_type="greater_or_equal", floor_strike=51.551), 9.0)
    assert e == {"kind": "strike", "ts": 9.0, "ticker": "KXSILVER15M-26NOV101900-00", "strike": 51.551, "sts": 1794354316000}
    r = kalshiws.parse_message(life("determined", result="no", settlement_value="0.0000"), 9.0)
    assert r["kind"] == "result" and r["result"] == "no"


def test_other_lifecycle_messages_are_ignored():
    assert kalshiws.parse_message(life("settled", settled_ts=1), 1.0) is None
    assert kalshiws.parse_message(life("metadata_updated"), 1.0) is None  # no strike in it
    assert kalshiws.parse_message(life("metadata_updated", floor_strike=0), 1.0) is None
    assert kalshiws.parse_message(life("determined", result="void"), 1.0) is None
    assert kalshiws.parse_message({"type": "market_lifecycle_v2", "msg": {"event_type": "metadata_updated", "floor_strike": 5}}, 1.0) is None  # no ticker


def test_the_live_stream_subscribes_and_the_extra_stream_does_not(monkeypatch):
    monkeypatch.delenv("KALSHI_LIFECYCLE", raising=False)
    live = kalshiws.KalshiStream(None, lambda e: None)
    extra = kalshiws.KalshiStream(None, lambda e: None, index_feed=False, fills=False)
    chan = lambda st: [c for cmd in st.subscriptions([]) for c in cmd["params"]["channels"]]  # noqa: E731
    assert "market_lifecycle_v2" in chan(live) and "market_lifecycle_v2" not in chan(extra)
    monkeypatch.setenv("KALSHI_LIFECYCLE", "0")
    assert "market_lifecycle_v2" not in chan(kalshiws.KalshiStream(None, lambda e: None))


def test_a_socket_target_fills_the_strike_store_for_15_minute_series_only():
    ticks.strikes.clear()
    added = []
    rec = SimpleNamespace(tape=SimpleNamespace(add=added.append), client=None, listeners=[])
    rec._on_lifecycle = lambda e: tape.TapeRecorder._on_lifecycle(rec, e)
    tape.TapeRecorder._on_event(rec, {"kind": "strike", "ts": 1.0, "ticker": "KXSILVER15M-26NOV101900-00", "strike": 51.551})
    tape.TapeRecorder._on_event(rec, {"kind": "strike", "ts": 1.0, "ticker": "KXLIGAPROMATCH-26NOV110545AKUBYS-AKU", "strike": 3.0})
    tape.TapeRecorder._on_event(rec, {"kind": "result", "ts": 1.0, "ticker": "KXSILVER15M-26NOV101845-45", "result": "no"})
    assert ticks.strikes.get("KXSILVER15M-26NOV101900-00") == 51.551 and ticks.strikes.get("KXLIGAPROMATCH-26NOV110545AKUBYS-AKU") is None
    assert added == []  # market facts are not written to the tape as prices
    ticks.strikes.clear()
