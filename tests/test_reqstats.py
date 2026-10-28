import logging
import threading

import httpx

from kalshi_moonshot import reqstats
from kalshi_moonshot.kalshi import KalshiClient


def setup_function():
    reqstats.flush()
    reqstats.set_source(None)


def test_tickers_and_ids_collapse_so_every_market_is_one_line():
    assert reqstats.shape("/markets/KXBTC15M-26OCT281300-00/orderbook") == "/markets/:id/orderbook"
    assert reqstats.shape("/markets/KXGOLD15M-26OCT281300-00") == "/markets/:id"
    assert reqstats.shape("/portfolio/fills?limit=100") == "/portfolio/fills"
    assert reqstats.shape("/portfolio/orders/0f8fad5b-d9cb-469f-a165-70867728950e") == "/portfolio/orders/:id"
    assert reqstats.shape("/events") == "/events"
    assert reqstats.shape("/series/KXBTC15M/markets/KXBTC15M-26OCT281300-00/candlesticks") == "/series/KXBTC15M/markets/:id/candlesticks"


def test_requests_are_counted_by_path_and_by_who_sent_them():
    reqstats.set_source("evaluate")
    reqstats.count("GET", "/markets/KXBTC15M-26OCT281300-00")
    reqstats.count("GET", "/markets/KXETH15M-26OCT281300-00")
    reqstats.set_source("lane_review:commodity")
    reqstats.count("GET", "/events")
    reqstats.set_source("evaluate")
    reqstats.count("POST", "/portfolio/events/orders")
    line = reqstats.flush()
    assert "4 in the last minute" in line and "3 reads, 1 writes" in line
    assert "GET /markets/:id 2" in line and "POST /portfolio/events/orders 1" in line
    assert "evaluate 3" in line and "lane_review:commodity 1" in line


def test_a_thread_outside_a_job_is_named_by_its_thread():
    seen = []
    th = threading.Thread(target=lambda: (reqstats.count("GET", "/events"), seen.append(reqstats.flush())), name="discovery")
    th.start()
    th.join()
    assert "discovery 1" in seen[0]


def test_flush_resets_and_is_empty_with_nothing_counted():
    reqstats.count("GET", "/events")
    assert reqstats.flush() != ""
    assert reqstats.flush() == ""


def test_the_client_counts_each_request_it_sends():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = KalshiClient("https://demo.test", max_rps=1000.0)
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    reqstats.set_source("scan")
    client._request("GET", "/markets/KXBTC15M-26OCT281300-00")
    client._request("GET", "/events")
    line = reqstats.flush()
    assert "2 in the last minute" in line and "scan 2" in line and "GET /markets/:id 1" in line


def test_the_logger_writes_the_line(caplog, monkeypatch):
    monkeypatch.setattr(reqstats, "EVERY", 0.0)
    reqstats.count("GET", "/events")
    calls = iter([False, True])
    with caplog.at_level(logging.INFO, logger="moonshot"):
        monkeypatch.setattr(reqstats.time, "sleep", lambda s: None)
        reqstats.log_loop(lambda: next(calls))
    assert any("kalshi requests: 1 in the last minute" in r.message for r in caplog.records)
