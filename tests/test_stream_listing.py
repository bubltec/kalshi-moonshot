"""The market list is kept from the stream's lifecycle events and does not expire on a clock (2026-11-14, user: "why would anything go stale? it would just mean you're not subscribed to the correct websocket events").
Unit: the parse, the cache's apply/serve rules, the reconnect rule. Integration: raw socket JSON through the stream, the tape recorder and the real client, with the REST layer made to fail."""

import calendar
import json
import threading
from types import SimpleNamespace

import pytest

from kalshi_moonshot import kalshiws, tape
from kalshi_moonshot.kalshi import KalshiClient
from kalshi_moonshot.marketdata import Discovery, MarketCache

T = "KXBTC15M-26NOV141200-00"
E = "KXBTC15M-26NOV141200"
CLOSE = calendar.timegm((2026, 11, 14, 17, 15, 0))  # epoch seconds: the close of the market below


class Clock:
    def __init__(self):
        self.t = float(CLOSE - 1800)

    def __call__(self):
        return self.t


def rest_row(ticker=T, event=E, close="2026-11-14T17:15:00Z"):
    return {"ticker": ticker, "event_ticker": event, "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.42", "status": "active", "close_time": close, "market_type": "binary"}


def created(ticker=T, close_ts=CLOSE, **extra):
    return {"kind": "market", "event": "created", "ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "title": "BTC", "sub": "Above", "rules": "r", "strike": 100.5, "cap": None, "open_ts": CLOSE - 900, "close_ts": close_ts, **extra}


def raw(event, **body):
    return json.dumps({"type": "market_lifecycle_v2", "sid": 6, "seq": 1, "sending_ts_ms": 1, "msg": {"event_type": event, "market_ticker": T, **body}})


# ---- parse ----
def test_created_carries_the_market_row_fields_from_additional_metadata():
    e = kalshiws.parse_message(json.loads(raw("created", open_ts=10, close_ts=20, additional_metadata={"event_ticker": E, "title": "BTC up?", "yes_sub_title": "Above 100", "rules_primary": "rules", "floor_strike": 100.5})), 5.0)
    assert e["kind"] == "market" and e["event"] == "created" and e["event_ticker"] == E and e["strike"] == 100.5 and (e["open_ts"], e["close_ts"]) == (10, 20) and e["title"] == "BTC up?"


@pytest.mark.parametrize("name", ["activated", "deactivated", "close_date_updated", "settled"])
def test_status_and_close_events_are_parsed_as_market_events(name):
    e = kalshiws.parse_message(json.loads(raw(name, close_ts=7, is_deactivated=True)), 1.0)
    assert e["kind"] == "market" and e["event"] == name and e["ticker"] == T


# ---- cache rules ----
def seeded_live(clock=None):
    clock = clock or Clock()
    cache = MarketCache(clock)
    cache.feed(True)
    clock.t += 1
    cache.put_series("KXBTC15M", [rest_row()])
    return cache, clock


def test_a_listing_read_after_the_confirmation_never_expires():
    cache, clock = seeded_live()
    clock.t += 600  # ten times the 60 s age limit, the market still open
    assert cache.get_series("KXBTC15M", 60) is not None and cache.get_event(E, 60) is not None and cache.live("KXBTC15M")


def test_a_listing_read_before_the_confirmation_is_not_trusted_and_ages_as_before():
    clock = Clock()
    cache = MarketCache(clock)
    cache.put_series("KXBTC15M", [rest_row()])
    clock.t += 1
    cache.feed(True)
    assert not cache.live("KXBTC15M")  # read before the stream was confirmed: events may have been missed
    clock.t += 500
    assert cache.get_series("KXBTC15M", 60) is None
    cache.put_series("KXBTC15M", [rest_row()])  # read again after: now it is kept
    assert cache.live("KXBTC15M")


def test_a_connection_that_ends_makes_every_listing_unproven():
    cache, clock = seeded_live()
    cache.feed(False)
    clock.t += 500
    assert not cache.live("KXBTC15M") and cache.get_series("KXBTC15M", 60) is None


def test_a_created_market_appears_in_the_live_listing_with_its_times_and_strike():
    cache, clock = seeded_live()
    nxt = "KXBTC15M-26NOV141215-15"
    cache.apply_market(created(nxt, close_ts=CLOSE + 900))
    row = next(m for m in cache.get_event("KXBTC15M-26NOV141215", 60) if m["ticker"] == nxt)
    assert row["floor_strike"] == 100.5 and row["status"] == "active" and row["close_time"].startswith("2026-11-14T") and row["series_ticker"] == "KXBTC15M"


def test_a_created_market_announced_while_the_listing_was_being_read_is_not_lost():
    clock = Clock()
    cache = MarketCache(clock)
    cache.feed(True)
    cache.apply_market(created())  # arrives before the seed lands
    clock.t += 1
    cache.put_series("KXBTC15M", [])  # the REST read began before the market existed
    assert [m["ticker"] for m in cache.get_series("KXBTC15M", 60)] == [T]


def test_status_close_and_strike_events_change_the_row_and_a_settlement_removes_it():
    cache, _ = seeded_live()
    cache.apply_market({"kind": "market", "event": "deactivated", "ticker": T})
    assert cache.get_series("KXBTC15M", 60)[0]["status"] == "inactive"
    cache.apply_market({"kind": "market", "event": "activated", "ticker": T})
    cache.apply_market({"kind": "market", "event": "close_date_updated", "ticker": T, "close_ts": CLOSE + 60})
    cache.apply_market({"kind": "strike", "ticker": T, "strike": 99.0})
    got = cache.get_series("KXBTC15M", 60)[0]
    assert got["status"] == "active" and got["floor_strike"] == 99.0 and got["close_time"].startswith("2026-11-14T")
    cache.apply_market({"kind": "result", "ticker": T, "result": "yes"})
    assert cache.get_series("KXBTC15M", 60) == []


def test_a_market_past_its_close_time_is_not_served_as_open_even_before_its_settlement_arrives():
    cache, clock = seeded_live()
    clock.t = CLOSE + 3600  # an hour after the close, settlement not yet announced
    assert cache.get_series("KXBTC15M", 60) == [] and cache.get_event(E, 60) is None


def test_the_stream_price_is_used_even_when_older_than_the_listing():
    cache, clock = seeded_live()
    cache.note_quote(T, 0.61, 0.63, ts=clock.t - 50)
    assert cache.get_series("KXBTC15M", 60)[0]["yes_ask_dollars"] == "0.6300"


def test_discovery_does_not_list_a_series_the_stream_keeps_and_lists_one_it_does_not():
    cache, clock = seeded_live()
    client = SimpleNamespace(cache=cache)
    d = Discovery(lambda: client, lambda: SimpleNamespace(), lambda s: set())
    assert d._fresh(client, "KXBTC15M", 150.0) is True  # kept by the stream
    assert d._fresh(client, "KXETH15M", 150.0) is False  # stream up, never read: read it now
    cache.feed(False)
    cache.put_series("KXBTC15M", [rest_row()])
    assert d._fresh(client, "KXBTC15M", 150.0) is True  # stream down: the age rule applies again, and a listing just read is young
    clock.t += 1000
    assert d._fresh(client, "KXBTC15M", 150.0) is False  # and an old one is read again


def test_confirming_the_stream_wakes_discovery_at_once():
    cache = MarketCache(Clock())
    d = Discovery(lambda: SimpleNamespace(cache=cache), lambda: SimpleNamespace(discovery_enabled=False), lambda s: set())
    cache.fed_listeners.append(lambda up: (d._wake.set(), d._wake_hot.set()) if up else None)
    cache.feed(True)
    assert d._wake.is_set() and d._wake_hot.is_set()


# ---- integration: raw socket JSON -> stream -> recorder -> cache -> the real client, with REST made to fail ----
def test_raw_lifecycle_messages_keep_the_clients_listing_current_with_no_rest_call():
    client = KalshiClient("https://example.test", max_rps=100)
    calls = []

    def request(method, path, params=None, **kw):
        calls.append(path)
        if path == "/markets" and (params or {}).get("series_ticker") == "KXBTC15M":
            return {"markets": [rest_row()], "cursor": ""}
        raise AssertionError(f"unexpected REST call {path}")

    clock = Clock()
    client.cache._clock = clock
    client._request = request
    rec = SimpleNamespace(tape=SimpleNamespace(add=lambda e: None), client=client, listeners=[], market_listeners=[], _wanted=set(), _refresh_wake=threading.Event(), stream=SimpleNamespace(_tickers=set(), set_tickers=lambda t: None))
    rec._follow_new = lambda t: tape.TapeRecorder._follow_new(rec, t)
    rec._feed_state = lambda up: tape.TapeRecorder._feed_state(rec, up)
    rec._on_lifecycle = lambda e: tape.TapeRecorder._on_lifecycle(rec, e)
    stream = kalshiws.KalshiStream(client, lambda e: tape.TapeRecorder._on_event(rec, e), on_feed=rec._feed_state)

    stream.handle(json.dumps({"type": "subscribed", "id": 6, "msg": {"channel": "market_lifecycle_v2", "sid": 6}}))
    assert client.cache.fed()
    with client.shared(180):
        assert [m["ticker"] for m in client.series_markets("KXBTC15M")] == [T]  # the one seed read
    seeded = len(calls)

    nxt = "KXBTC15M-26NOV141215-15"
    stream.handle(json.dumps({"type": "market_lifecycle_v2", "sid": 6, "seq": 2, "msg": {"event_type": "created", "market_ticker": nxt, "open_ts": CLOSE, "close_ts": CLOSE + 900,
                                                                                         "additional_metadata": {"event_ticker": "KXBTC15M-26NOV141215", "floor_strike": 77.0, "title": "t"}}}))
    clock.t += 600  # ten minutes, far past the 180 s age limit, and the next market has not closed
    with client.shared(180):
        listed = client.series_markets("KXBTC15M")
        ev = client.event_markets("KXBTC15M-26NOV141215")
    assert nxt in [m["ticker"] for m in listed] and [m["floor_strike"] for m in ev] == [77.0]
    assert len(calls) == seeded  # nothing went to Kalshi after the seed

    stream._ws = None
    rec._feed_state(False)  # the connection ended
    assert not client.cache.live("KXBTC15M")
