import asyncio
import json
import threading
import time

import pytest

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


# ---- the perps websocket (2026-11-13, user: "move all the recording for the Kalshi perp to the websocket. It should be on its own threads") ----



def ticker_msg(ticker="KXBTCPERP", price="6.8201", ts_ms=1794628481000, **extra):
    return {"type": "ticker", "sid": 1, "msg": {"market_ticker": ticker, "price": "6.8", "bid": "6.81", "ask": "6.83", "reference_price": {"price": price, "ts_ms": ts_ms}, **extra}}


SIZES = {"KXBTCPERP": 0.0001, "KXGOLDPERP": 0.001}


def test_unit_contract_sizes_are_read_for_the_perps_we_record_only():
    got = perpref.contract_sizes([{"ticker": "KXBTCPERP", "contract_size": "0.0001"}, {"ticker": "KXNOPEPERP", "contract_size": "1"}, {"ticker": "KXGOLDPERP"}, {"ticker": "KXETHPERP", "contract_size": "0"}])
    assert got == {"KXBTCPERP": 0.0001}


def test_unit_a_ticker_message_becomes_an_index_event_in_the_underlyings_units():
    e = perpref.parse_ticker(json.dumps(ticker_msg()), SIZES, 100.0)
    assert e["kind"] == "index" and e["index_id"] == "KP_BTC" and e["value"] == pytest.approx(68201.0) and e["src_ms"] == 1794628481000 and e["ts"] == 100.0
    assert perpref.parse_ticker(json.dumps(ticker_msg()).encode(), SIZES, 100.0)["index_id"] == "KP_BTC"  # bytes too


def test_unit_messages_that_carry_no_usable_reading_are_dropped():
    assert perpref.parse_ticker({"type": "subscribed", "id": 1, "msg": {"channel": "ticker", "sid": 1}}, SIZES, 1.0) is None  # the ack
    assert perpref.parse_ticker(ticker_msg(ticker="KXVVVPERP"), SIZES, 1.0) is None  # a perp we do not record
    assert perpref.parse_ticker(ticker_msg(ticker="KXETHPERP"), SIZES, 1.0) is None  # no contract size known yet
    no_ref = ticker_msg()
    del no_ref["msg"]["reference_price"]
    assert perpref.parse_ticker(no_ref, SIZES, 1.0) is None  # the reference price is optional on the channel
    assert perpref.parse_ticker(ticker_msg(price="0"), SIZES, 1.0) is None and perpref.parse_ticker("not json", SIZES, 1.0) is None


def test_unit_a_socket_reading_is_recorded_once_per_source_timestamp():
    got = []
    f = perpref.PerpRefFeed(got.append)
    f.sizes = dict(SIZES)
    assert f.handle_ticker(ticker_msg(ts_ms=1000)) and not f.handle_ticker(ticker_msg(ts_ms=1000))  # the same reading again (a stale reference outside its hours)
    assert f.handle_ticker(ticker_msg(ts_ms=2000, price="6.8300")) and [round(e["value"]) for e in got] == [68201, 68300]


class Authed:
    authed = True
    base_url = "https://api.elections.kalshi.com/trade-api/v2"

    def ws_headers(self, url):
        return {}


def test_unit_with_credentials_the_feed_is_a_socket_on_its_own_thread_and_without_them_it_polls():
    sock = perpref.PerpRefFeed(lambda e: None, client=Authed())
    assert sock.mode == "socket" and sock.ws_url == perpref.WS_URL
    demo = type("D", (Authed,), {"base_url": "https://demo-api.kalshi.co/trade-api/v2"})()
    assert perpref.PerpRefFeed(lambda e: None, client=demo).ws_url == perpref.WS_URL_DEMO
    assert perpref.PerpRefFeed(lambda e: None).mode == "poll" and perpref.PerpRefFeed(lambda e: None, client=type("N", (), {"authed": False})()).mode == "poll"
    sub = sock.subscription()
    assert sub["cmd"] == "subscribe" and sub["params"]["channels"] == ["ticker"] and set(sub["params"]["market_tickers"]) == set(perpref.FEEDS.values())


def serve(script):
    """A local websocket server on its own thread that runs `script(ws)` for each connection. Returns (url, list of subscribe messages it received, stop)."""
    import websockets

    seen, ready, box = [], threading.Event(), {}

    async def handler(ws):
        seen.append(json.loads(await ws.recv()))
        await script(ws, len(seen))

    async def main():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            box["port"] = server.sockets[0].getsockname()[1]
            box["stop"] = asyncio.get_running_loop().create_future()
            box["loop"] = asyncio.get_running_loop()
            ready.set()
            await box["stop"]

    threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()
    assert ready.wait(5)
    return f"ws://127.0.0.1:{box['port']}", seen, lambda: box["loop"].call_soon_threadsafe(box["stop"].set_result, None)


def wait_for(cond, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_integration_readings_arrive_from_the_socket_on_the_feeds_own_thread_and_nothing_is_polled(monkeypatch):
    async def script(ws, n):
        await ws.send(json.dumps({"type": "subscribed", "id": 1, "msg": {"channel": "ticker", "sid": 1}}))
        await ws.send(json.dumps(ticker_msg(ts_ms=1000)))
        await ws.send(json.dumps(ticker_msg(ticker="KXGOLDPERP", price="3.8725", ts_ms=1000)))
        await ws.send(json.dumps(ticker_msg(ts_ms=2000, price="6.8300")))
        await asyncio.sleep(30)

    url, seen, stop = serve(script)
    polls = []
    monkeypatch.setattr(perpref.httpx.Client, "get", lambda self, *a, **k: polls.append(a) or (_ for _ in ()).throw(AssertionError("the socket feed must not poll")))
    got, threads = [], []
    f = perpref.PerpRefFeed(lambda e: (got.append(e), threads.append(threading.current_thread().name)), client=Authed(), ws_url=url)
    f.sizes = dict(SIZES)  # as the one read at the start leaves them
    f.start()
    try:
        assert wait_for(lambda: len(got) == 3)
        assert [(e["index_id"], round(e["value"], 1)) for e in got] == [("KP_BTC", 68201.0), ("KP_GOLD", 3872.5), ("KP_BTC", 68300.0)]
        assert set(threads) == {"perpref-ws"} and polls == []
        assert seen[0]["params"]["channels"] == ["ticker"] and "KXBTCPERP" in seen[0]["params"]["market_tickers"]
    finally:
        f.close()
        stop()


def test_integration_the_feed_reconnects_and_subscribes_again_when_the_socket_drops(monkeypatch):
    async def script(ws, n):
        await ws.send(json.dumps(ticker_msg(ts_ms=1000 * n)))
        if n == 1:
            await ws.close()  # the first connection drops after one reading
        else:
            await asyncio.sleep(30)

    url, seen, stop = serve(script)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(perpref.asyncio, "sleep", lambda s: real_sleep(min(s, 0.05)))  # the back-off, shortened for the test
    got = []
    f = perpref.PerpRefFeed(got.append, client=Authed(), ws_url=url)
    f.sizes = dict(SIZES)
    f.start()
    try:
        assert wait_for(lambda: len(got) == 2 and len(seen) == 2)
        assert [e["src_ms"] for e in got] == [1000, 2000]
    finally:
        f.close()
        stop()


def test_integration_the_contract_sizes_are_read_once_before_the_socket_opens(monkeypatch):
    async def script(ws, n):
        await ws.send(json.dumps(ticker_msg(ts_ms=1000)))
        await asyncio.sleep(30)

    url, _seen, stop = serve(script)
    calls = []

    class Resp:
        status_code = 200

        def json(self):
            return {"markets": [{"ticker": "KXBTCPERP", "contract_size": "0.0001"}]}

    monkeypatch.setattr(perpref.httpx.Client, "get", lambda self, *a, **k: calls.append(1) or Resp())
    got = []
    f = perpref.PerpRefFeed(got.append, client=Authed(), ws_url=url)
    f.start()
    try:
        assert wait_for(lambda: len(got) == 1)
        time.sleep(0.3)
        assert len(calls) == 1 and got[0]["value"] == pytest.approx(68201.0)  # one read of the list, then the socket only
    finally:
        f.close()
        stop()
