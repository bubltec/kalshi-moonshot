import argparse

import pytest

from kalshi_moonshot import __main__ as main
from kalshi_moonshot import config
from kalshi_moonshot.kalshi import KalshiClient


class Fake:
    sent = None

    def balance(self):
        return {"balance_breakdown": [{"balance": "0", "exchange_index": 0}]}

    def shard_transfer(self, usd, source, source_shard, destination, destination_shard):
        Fake.sent = (usd, source, source_shard, destination, destination_shard)
        return {"transfer_id": "t1"}


def args(**kw):
    base = {"source": "event:2", "dest": "event:0", "usd": 15.0, "max_usd": 50.0, "yes": False}
    return argparse.Namespace(**{**base, **kw})


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    Fake.sent = None
    monkeypatch.setattr(main, "_client", lambda s: Fake())


def test_without_yes_nothing_is_sent():
    assert main.cmd_shardmove(config.load(), args()) == 0
    assert Fake.sent is None


def test_yes_sends_the_transfer():
    main.cmd_shardmove(config.load(), args(yes=True))
    assert Fake.sent == (15.0, "event_contract", 2, "event_contract", 0)


def test_guards_refuse_bad_amounts_and_no_op_moves():
    for bad in (args(usd=0.0), args(usd=500.0), args(source="event:0", dest="event:0"), args(source="bank:2")):
        with pytest.raises(SystemExit):
            main.cmd_shardmove(config.load(), bad)
    assert Fake.sent is None


def test_the_request_body_is_in_centicents(monkeypatch):
    seen = {}

    def fake_request(self, method, path, **kw):
        seen.update(method=method, path=path, **kw)
        return {"transfer_id": "x"}

    monkeypatch.setattr(KalshiClient, "_request", fake_request)
    monkeypatch.setattr(KalshiClient, "__init__", lambda self: None)
    KalshiClient().shard_transfer(15.0)
    assert seen["path"] == "/portfolio/intra_exchange_instance_transfer" and seen["json"]["amount"] == 150_000
    assert seen["json"]["source_exchange_shard"] == 2 and seen["json"]["destination_exchange_shard"] == 0
