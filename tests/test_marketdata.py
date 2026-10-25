import threading

from kalshi_moonshot import config
from kalshi_moonshot.kalshi import KalshiClient
from kalshi_moonshot.marketdata import Discovery, MarketCache
from kalshi_moonshot.worker import Worker


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def row(ticker, event, bid="0.40", ask="0.42"):
    return {"ticker": ticker, "event_ticker": event, "yes_bid_dollars": bid, "yes_ask_dollars": ask, "status": "active", "close_time": "2026-10-25T01:00:00Z"}


def test_a_listing_is_served_from_memory_while_young_and_as_independent_copies():
    clock = Clock()
    cache = MarketCache(clock)
    cache.put_series("KXBTC15M", [row("A-1", "A"), row("B-1", "B")])
    first = cache.get_series("KXBTC15M", 60)
    first[0]["category"] = "Crypto"  # a lane adds its own fields to the rows it reads
    assert "category" not in cache.get_series("KXBTC15M", 60)[0]
    clock.t += 61
    assert cache.get_series("KXBTC15M", 60) is None and cache.hits == 2 and cache.misses == 1


def test_an_event_and_a_market_are_found_inside_a_series_listing():
    cache = MarketCache(Clock())
    cache.put_series("KXBTCD", [row("E1-T1", "E1"), row("E1-T2", "E1"), row("E2-T1", "E2")])
    assert [m["ticker"] for m in cache.get_event("E1", 60)] == ["E1-T1", "E1-T2"]
    assert cache.get_market("E2-T1", 60)["event_ticker"] == "E2" and cache.get_market("nope", 60) is None


def test_a_newer_streamed_quote_replaces_the_price_on_the_cached_row_and_an_older_one_does_not():
    clock = Clock()
    cache = MarketCache(clock)
    cache.put_series("S", [row("A-1", "A")])
    clock.t += 10
    cache.note_quote("A-1", 0.55, 0.57, ts=clock.t)
    got = cache.get_series("S", 60)[0]
    assert (got["yes_bid_dollars"], got["yes_ask_dollars"]) == ("0.5500", "0.5700")
    cache.note_quote("A-1", 0.10, 0.12, ts=clock.t - 100)  # older than the listing, but it arrives later: the newer stored one wins only if it is newer than the fetch
    cache.put_series("S", [row("A-1", "A", "0.30", "0.32")])
    assert cache.get_series("S", 60)[0]["yes_bid_dollars"] == "0.30"  # a fresh listing is newer than the stored quote
    cache.note_quote("A-1", None, 0.5)  # a one-sided quote is ignored
    assert cache.get_series("S", 60)[0]["yes_bid_dollars"] == "0.30"


def test_a_market_in_an_old_listing_is_still_usable_when_the_stream_keeps_its_price_current():
    clock = Clock()
    cache = MarketCache(clock)
    cache.put_series("S", [row("A-1", "A"), row("B-1", "B")])
    clock.t += 300  # the listing is 5 minutes old, past max_age
    assert cache.get_market("A-1", 60) is None  # no streamed quote: not trusted
    cache.note_quote("A-1", 0.61, 0.63, ts=clock.t - 5)
    got = cache.get_market("A-1", 60)
    assert got is not None and got["yes_ask_dollars"] == "0.6300"
    assert cache.get_event("B", 60) is None  # B has no streamed quote


class FakeRequests:
    def __init__(self, client):
        self.calls = []
        client._request = self.__call__

    def __call__(self, method, path, params=None, **kw):
        self.calls.append((path, (params or {}).get("series_ticker") or (params or {}).get("event_ticker")))
        if path.startswith("/markets/"):
            return {"market": row(path.split("/")[-1], "E1")}
        series = (params or {}).get("series_ticker")
        if series:
            return {"markets": [row("E1-T1", "E1"), row("E1-T2", "E1")], "cursor": ""}
        return {"markets": [row("E1-T1", "E1"), row("E1-T2", "E1")], "cursor": ""}


def client():
    c = KalshiClient("https://example.test", max_rps=100)
    return c, FakeRequests(c)


def test_without_the_shared_block_every_call_goes_to_kalshi_but_still_fills_the_cache():
    c, calls = client()
    c.series_markets("KXBTC15M")
    c.series_markets("KXBTC15M")
    assert len(calls.calls) == 2
    with c.shared(60):
        c.series_markets("KXBTC15M")
    assert len(calls.calls) == 2  # now served from what the earlier calls stored


def test_two_hundred_lanes_reading_the_same_market_cost_one_request():
    c, calls = client()
    c.series_markets("KXBTC15M")  # the discovery thread's one listing
    before = len(calls.calls)
    with c.shared(60):
        for _ in range(200):  # 200 lanes' scope and review and paper evaluation, all of them reading the same data
            assert len(c.series_markets("KXBTC15M")) == 2
            assert len(c.event_markets("E1")) == 2
            assert c.market("E1-T1")["ticker"] == "E1-T1"
    assert len(calls.calls) == before


def test_the_shared_block_nests_and_does_not_leak_to_other_threads():
    c, calls = client()
    c.series_markets("S")
    seen = []
    with c.shared(60):
        assert c._age() == 60
        with c.shared(0):  # a live lane's evaluation asks Kalshi
            assert c._age() == 0
            c.market("E1-T1")
        assert c._age() == 60
        t = threading.Thread(target=lambda: seen.append(c._age()))
        t.start()
        t.join()
    assert seen == [0.0] and c._age() == 0.0
    assert ("/markets/E1-T1", None) in calls.calls  # the live read went to Kalshi although the listing was cached


def test_discovery_lists_each_wanted_series_once_per_cycle_and_survives_a_failure(monkeypatch):
    class C:
        def __init__(self):
            self.asked = []

        def series_markets(self, series):
            self.asked.append(series)
            if series == "BAD":
                raise RuntimeError("boom")
            return []

    c = C()
    d = Discovery(lambda: c, lambda: config.load(), lambda s: {"KXETH15M", "KXBTC15M", "BAD"})
    monkeypatch.setattr(d._stop, "wait", lambda t=None: False)  # the real cycle pauses between requests; the test must not
    assert d.cycle() == 2 and c.asked == ["BAD", "KXBTC15M", "KXETH15M"]


def test_the_worker_reads_the_cache_only_when_discovery_is_on(tmp_path, monkeypatch):
    from kalshi_moonshot.store import Store

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    c, _ = client()
    w = Worker(config.load(), c, None, Store(tmp_path / "m.db", frozenset()), None)
    with w._shared(30):
        assert c._age() == 30
    with w._shared(0):
        assert c._age() == 0
    monkeypatch.setenv("DISCOVERY_ENABLED", "false")
    w.s = config.load()
    with w._shared(30):
        assert c._age() == 0


def test_discovery_spaces_its_requests_across_the_cycle(monkeypatch):
    monkeypatch.setenv("DISCOVERY_SECONDS", "50")

    class C:
        def series_markets(self, series):
            return []

    d = Discovery(lambda: C(), lambda: config.load(), lambda s: {"A", "B", "C", "D", "E"})
    waits = []
    monkeypatch.setattr(d._stop, "wait", lambda t=None: waits.append(t) or False)
    assert d.cycle() == 5
    assert waits == [8.0] * 5  # 50 s x 0.8 / 5 series: the request budget is never held for a burst


def test_the_stop_backstop_reads_paper_bets_from_the_cache_but_real_ones_from_kalshi(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from kalshi_moonshot import runner
    from kalshi_moonshot.store import Store

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    store = Store(s.db_path, frozenset())
    for ticker, mode in (("KXBTCD-A", "observe"), ("KXBTCD-B", "trade")):
        store.db.execute(
            "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (1.0, ticker, "bet", mode, 0.80, "yes", "commodity", 6, 4.9, "2099-01-01T00:00:00Z"),
        )
    store.db.commit()

    class C:
        def __init__(self):
            self.age, self.reads = 0.0, {}

        @contextmanager
        def shared(self, age):
            prev, self.age = self.age, age
            try:
                yield
            finally:
                self.age = prev

        def market(self, ticker):
            self.reads[ticker] = self.age
            return {"ticker": ticker, "yes_bid_dollars": "0.78", "yes_ask_dollars": "0.80", "close_time": "2099-01-01T00:00:00Z"}

    c = C()
    runner.stop_loss_pass(s, c, store)
    assert c.reads == {"KXBTCD-A": 10.0, "KXBTCD-B": 0.0}
