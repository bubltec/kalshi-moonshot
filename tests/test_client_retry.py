import httpx
import pytest

from kalshi_moonshot import kalshi
from kalshi_moonshot.kalshi import KalshiClient, KalshiError, KalshiRateLimited


def client_with(handler, max_rps=1000.0):
    c = KalshiClient("https://example.test/v2", max_rps=max_rps)
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


@pytest.fixture
def sleeps(monkeypatch):
    got: list[float] = []
    monkeypatch.setattr(kalshi.time, "sleep", got.append)
    return got


def test_429_waits_for_retry_after_then_succeeds(sleeps):
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"ok": True})

    assert client_with(handler)._request("GET", "/x") == {"ok": True}
    assert len(calls) == 2
    assert max(sleeps) >= 3.0


def test_persistent_429_raises_rate_limited_with_a_wait(sleeps):
    c = client_with(lambda req: httpx.Response(429))
    with pytest.raises(KalshiRateLimited) as err:
        c._request("GET", "/x")
    assert err.value.retry_after >= 30


def test_reads_retry_server_errors_and_timeouts(sleeps):
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503)
        if len(calls) == 2:
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, json={"n": len(calls)})

    assert client_with(handler)._request("GET", "/x") == {"n": 3}


def test_writes_are_not_resent_after_a_server_error_or_timeout(sleeps):
    calls = []

    def boom(req):
        calls.append(1)
        return httpx.Response(503)

    with pytest.raises(KalshiError):
        client_with(boom)._request("POST", "/order")
    assert len(calls) == 1

    calls.clear()

    def timeout(req):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(KalshiError):
        client_with(timeout)._request("POST", "/order")
    assert len(calls) == 1


def test_a_refused_write_429_is_retried(sleeps):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429) if len(calls) == 1 else httpx.Response(200, json={"ok": 1})

    assert client_with(handler)._request("POST", "/order") == {"ok": 1}


def test_requests_are_spaced_out(sleeps):
    c = client_with(lambda req: httpx.Response(200, json={}), max_rps=2.0)
    c._request("GET", "/a")
    c._request("GET", "/b")
    assert any(s >= 0.4 for s in sleeps)  # 2 per second means 0.5s apart


def test_client_error_is_not_retried(sleeps):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(404, text="nope")

    with pytest.raises(KalshiError):
        client_with(handler)._request("GET", "/x")
    assert len(calls) == 1
