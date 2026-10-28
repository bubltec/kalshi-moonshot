import threading
import time

import httpx

from kalshi_moonshot import kalshi, runner
from kalshi_moonshot.kalshi import KalshiClient
from kalshi_moonshot.store import Store


def fresh_client(handler):
    kalshi._EVENT_CACHE.clear()
    client = KalshiClient("https://demo.test", max_rps=1000.0)
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_many_threads_asking_for_the_event_list_page_it_once():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("cursor") or "first")
        time.sleep(0.01)
        if not request.url.params.get("cursor"):
            return httpx.Response(200, json={"events": [{"event_ticker": "E1", "category": "Crypto", "series_ticker": "S", "mutually_exclusive": False}], "cursor": "c2"})
        return httpx.Response(200, json={"events": [{"event_ticker": "E2", "category": "Crypto", "series_ticker": "S", "mutually_exclusive": False}], "cursor": ""})

    client = fresh_client(handler)
    results = []
    threads = [threading.Thread(target=lambda: results.append(client.event_info())) for _ in range(12)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(calls) == 2  # two pages, once, not twelve times
    assert all(set(r) == {"E1", "E2"} for r in results)


def test_a_second_client_reuses_the_same_event_list():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"events": [{"event_ticker": "E1", "category": "Crypto", "series_ticker": "S", "mutually_exclusive": False}], "cursor": ""})

    first = fresh_client(handler)
    first.event_info()
    second = KalshiClient("https://demo.test", max_rps=1000.0)
    second._http = first._http
    assert set(second.event_info()) == {"E1"} and len(calls) == 1


def test_a_static_market_read_is_kept_and_a_normal_one_is_not():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"market": {"ticker": "T", "close_time": "2099-01-01T00:00:00Z"}})

    client = fresh_client(handler)
    for _ in range(5):
        assert client.market_static("T")["close_time"] == "2099-01-01T00:00:00Z"
    assert len(calls) == 1  # one request for five reads of the fixed fields
    client.market("T")
    client.market("T")
    assert len(calls) == 3  # a live read still asks every time


def test_markets_are_read_in_batches():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        wanted = request.url.params["tickers"].split(",")
        seen.append(len(wanted))
        return httpx.Response(200, json={"markets": [{"ticker": t, "result": "yes"} for t in wanted]})

    client = fresh_client(handler)
    assert [m["ticker"] for m in client.markets_by_tickers(["A", "B", "C"])] == ["A", "B", "C"] and seen == [3]


def test_resolve_asks_for_the_settled_markets_in_a_few_requests(tmp_path):
    store = Store(tmp_path / "m.db")
    for i in range(250):
        store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane) VALUES (?,?,?,?,?,?,?)", (1.0, f"KX{i}-A", "bet", "observe", 0.5, "yes", "x"))
    store.db.commit()
    asked = []

    class Venue:
        def markets_by_tickers(self, tickers):
            asked.append(len(tickers))
            return [{"ticker": t, "result": "yes" if t != "KX7-A" else ""} for t in tickers]  # one market still open

        def market(self, ticker):
            raise AssertionError("every ticker came back in a batch: nothing should be read one by one")

    n = runner.resolve(Venue(), store)
    assert n == 249 and asked == [100, 100, 50]  # 250 tickers in 3 requests, not 250


def test_resolve_falls_back_to_a_single_read_for_a_market_a_batch_left_out(tmp_path):
    store = Store(tmp_path / "m.db")
    store.db.execute("INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane) VALUES (?,?,?,?,?,?,?)", (1.0, "LEFT-OUT", "bet", "observe", 0.5, "yes", "x"))
    store.db.commit()

    class Venue:
        def markets_by_tickers(self, tickers):
            return []

        def market(self, ticker):
            return {"ticker": ticker, "result": "no"}

    assert runner.resolve(Venue(), store) == 1
