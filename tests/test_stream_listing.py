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
    cache, _clock = seeded_live()
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


def test_an_event_for_a_market_the_listing_does_not_hold_makes_the_listing_unproven():
    """The 08:15 live miss: the next window arrived as a strike event with no created event, so the kept listing never held it."""
    cache, _clock = seeded_live()
    assert cache.live("KXBTC15M")
    cache.apply_market({"kind": "market", "event": "activated", "ticker": "KXBTC15M-26NOV141215-15"})  # a market it holds no row for, and no strike to build one from
    assert not cache.live("KXBTC15M")
    assert cache.get_series("KXBTC15M", 180) is None and cache.get_event(E, 180) is None  # even a young listing is asked for again
    cache.put_series("KXBTC15M", [rest_row(), rest_row("KXBTC15M-26NOV141215-15", "KXBTC15M-26NOV141215", "2026-11-14T17:30:00Z")])
    assert cache.live("KXBTC15M") and len(cache.get_series("KXBTC15M", 180)) == 2


def test_a_strike_event_for_a_new_window_builds_its_row_and_keeps_the_listing_proven():
    """The 08:30 live miss: the REST listing trailed the stream, so re-reading it on the first strike found nothing new. The strike and the ticker carry what a lane needs."""
    cache, _ = seeded_live()
    nxt = "KXBTC15M-26NOV141215-15"
    cache.apply_market({"kind": "strike", "ticker": nxt, "strike": 66915.52})
    assert cache.live("KXBTC15M")
    row = next(m for m in cache.get_event("KXBTC15M-26NOV141215", 180) if m["ticker"] == nxt)
    assert row["floor_strike"] == 66915.52 and row["status"] == "active" and row["close_time"] == "2026-11-14T17:15:00Z" and row["series_ticker"] == "KXBTC15M"
    cache.put_series("KXBTC15M", [rest_row()])  # a later listing that does not have it yet does not lose it
    assert nxt in [m["ticker"] for m in cache.get_series("KXBTC15M", 180)]
    cache.apply_market({"kind": "result", "ticker": nxt, "result": "yes"})
    assert nxt not in [m["ticker"] for m in cache.get_series("KXBTC15M", 180)]


def test_a_ticker_without_a_close_time_builds_no_row():
    assert MarketCache._close_from_ticker("KXBTC15M-26NOV140845-45") == "2026-11-14T13:45:00Z"
    assert MarketCache._close_from_ticker("KXFOO") == "" and MarketCache._close_from_ticker("KXBTC15M-26XXX140845-45") == ""


def test_an_event_for_a_market_it_does_hold_keeps_the_listing_proven():
    cache, _ = seeded_live()
    cache.apply_market({"kind": "strike", "ticker": T, "strike": 7.0})
    cache.apply_market({"kind": "market", "event": "activated", "ticker": T})
    assert cache.live("KXBTC15M")


def test_the_next_window_arriving_as_a_bare_strike_event_reaches_the_client_with_no_rest_read():
    client = KalshiClient("https://example.test", max_rps=100)
    clock = Clock()
    client.cache._clock = clock
    nxt = "KXBTC15M-26NOV141215-15"
    state = {"listed": [rest_row()], "calls": 0}

    def request(method, path, params=None, **kw):
        state["calls"] += 1
        return {"markets": list(state["listed"]), "cursor": ""}

    client._request = request
    rec = SimpleNamespace(tape=SimpleNamespace(add=lambda e: None), client=client, listeners=[], market_listeners=[], _wanted=set(), _refresh_wake=threading.Event(), stream=SimpleNamespace(_tickers=set(), set_tickers=lambda t: None))
    rec._feed_state = lambda up: tape.TapeRecorder._feed_state(rec, up)
    rec._follow_new = lambda t: tape.TapeRecorder._follow_new(rec, t)
    rec._on_lifecycle = lambda e: tape.TapeRecorder._on_lifecycle(rec, e)
    stream = kalshiws.KalshiStream(client, lambda e: tape.TapeRecorder._on_event(rec, e), on_feed=rec._feed_state)
    stream.handle(json.dumps({"type": "subscribed", "id": 6, "msg": {"channel": "market_lifecycle_v2", "sid": 6}}))
    with client.shared(180):
        client.series_markets("KXBTC15M")
    assert state["calls"] == 1
    state["listed"] = [rest_row(), rest_row(nxt, nxt.rsplit("-", 1)[0], "2026-11-14T17:30:00Z")]
    stream.handle(json.dumps({"type": "market_lifecycle_v2", "sid": 6, "seq": 3, "msg": {"event_type": "metadata_updated", "market_ticker": nxt, "floor_strike": 99.0}}))
    with client.shared(180):
        got = [m["ticker"] for m in client.series_markets("KXBTC15M")]
        again = [m["ticker"] for m in client.series_markets("KXBTC15M")]
    assert nxt in got and got == again and state["calls"] == 1  # the strike built the row: no read of Kalshi, whose listing trails the stream


def test_a_row_built_from_the_stream_can_be_priced_by_the_ladder():
    """The live miss from 08:45: windows built from strike and created events had no strike_type, so commodities.market_prob priced nothing and the lane's screen dropped every one as 'could not price this ladder'."""
    from kalshi_moonshot import commodities

    cache, _ = seeded_live()
    nxt = "KXBTC15M-26NOV141215-15"
    cache.apply_market({"kind": "strike", "ticker": nxt, "strike": 100.0})
    built = next(m for m in cache.get_series("KXBTC15M", 180) if m["ticker"] == nxt)
    assert built["strike_type"] == "greater_or_equal"
    p = commodities.market_prob(built, 101.0, 0.5)
    assert p is not None and 0.5 < p <= 1.0  # the price is above the strike: YES is the likelier side
    assert commodities.market_prob(built, 99.0, 0.5) < 0.5
    cache.apply_market(created("KXBTC15M-26NOV141230-30", strike_type="greater_or_equal"))
    row = next(m for m in cache.get_series("KXBTC15M", 180) if m["ticker"] == "KXBTC15M-26NOV141230-30")
    assert row["strike_type"] == "greater_or_equal" and commodities.market_prob(row, 101.0, 0.5) is not None


def test_the_streams_own_strike_type_is_kept_and_parsed():
    e = kalshiws.parse_message(json.loads(raw("created", open_ts=10, close_ts=20, additional_metadata={"event_ticker": E, "floor_strike": 5.0, "strike_type": "less"})), 1.0)
    assert e["strike_type"] == "less"
    s = kalshiws.parse_message(json.loads(raw("metadata_updated", floor_strike=5.0, strike_type="greater")), 1.0)
    assert s["strike_type"] == "greater"
    cache, _ = seeded_live()
    cache.apply_market({"kind": "strike", "ticker": "KXBTC15M-26NOV141215-15", "strike": 5.0, "strike_type": "less"})
    assert next(m for m in cache.get_series("KXBTC15M", 180) if m["ticker"] == "KXBTC15M-26NOV141215-15")["strike_type"] == "less"
