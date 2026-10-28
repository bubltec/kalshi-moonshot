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
