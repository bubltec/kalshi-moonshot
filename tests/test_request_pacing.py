from kalshi_moonshot import kalshi
from kalshi_moonshot.kalshi import KalshiClient


def client():
    return KalshiClient("https://demo.test", max_rps=8.0)


def test_an_order_does_not_wait_behind_a_queue_of_reads(monkeypatch):
    slept = []
    monkeypatch.setattr(kalshi.time, "sleep", lambda s: slept.append(s))
    c = client()
    for _ in range(40):
        c._pace()  # a page of market reads queues up: later reads wait their turn
    assert max(slept) > 3.0
    slept.clear()
    c._pace(write=True)
    assert slept == [] or max(slept) < 0.01  # a sale takes its own turn, not the reads'


def test_orders_are_still_spaced_apart_from_each_other(monkeypatch):
    slept = []
    monkeypatch.setattr(kalshi.time, "sleep", lambda s: slept.append(s))
    c = client()
    for _ in range(10):
        c._pace(write=True)
    assert max(slept) > 0.5  # ten orders at once still queue at the request budget


def test_a_429_on_a_write_slows_writes_not_reads(monkeypatch):
    slept = []
    monkeypatch.setattr(kalshi.time, "sleep", lambda s: slept.append(s))
    c = client()
    c._pace(extra=5.0, write=True)
    slept.clear()
    c._pace()
    assert slept == [] or max(slept) < 0.01


def test_a_429_makes_only_that_call_wait_and_it_is_retried(monkeypatch):
    import httpx

    slept = []
    monkeypatch.setattr(kalshi.time, "sleep", lambda s: slept.append(s))
    answers = iter([httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json={"ok": True})])

    client = KalshiClient("https://demo.test", max_rps=1000.0)
    client._http = httpx.Client(transport=httpx.MockTransport(lambda request: next(answers)))
    assert client._request("GET", "/events") == {"ok": True}  # retried and succeeded
    assert any(1.9 <= s <= 2.6 for s in slept)  # this caller waited out Retry-After (2 s plus jitter)
    assert client._next_slot - kalshi.time.monotonic() < 0.5 and client._next_write_slot == 0.0  # no wait was pushed onto anyone else's turn


def test_a_429_on_an_order_is_retried_with_the_same_client_order_id(monkeypatch):
    import json as jsonlib

    import httpx

    monkeypatch.setattr(kalshi.time, "sleep", lambda s: None)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(jsonlib.loads(request.content)["client_order_id"])
        return httpx.Response(429) if len(seen) == 1 else httpx.Response(200, json={"order_id": "o1"})

    client = KalshiClient("https://demo.test", max_rps=1000.0)
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    client._request("POST", "/portfolio/events/orders", json={"client_order_id": "abc"})
    assert seen == ["abc", "abc"]  # a 429 means refused, so the same id is safe to send again
