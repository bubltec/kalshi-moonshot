import json

from kalshi_moonshot import config
from kalshi_moonshot.futflow import FuturesFlow, parse_trades
from kalshi_moonshot.tape import Tape


def msg(trades, snapshot=False):
    out = {"channel": "trades", "data": trades}
    if snapshot:
        out["isSnapshot"] = True
    return out


def trade(coin="BTC", side="B", px="80000.5", sz="0.01", t=1_800_000_000_250):
    return {"coin": coin, "side": side, "px": px, "sz": sz, "time": t, "tid": 1, "hash": "0x"}


def test_a_live_trades_message_becomes_one_tuple_per_trade_with_the_takers_side():
    got = parse_trades(msg([trade(side="B"), trade(side="A", px="80001", sz="0.5")]))
    assert got == [("BTC", 1_800_000_000.25, 80000.5, 0.01, True), ("BTC", 1_800_000_000.25, 80001.0, 0.5, False)]


def test_snapshots_other_channels_and_malformed_trades_are_ignored():
    assert parse_trades(msg([trade()], snapshot=True)) == []
    assert parse_trades({"channel": "allMids", "data": {}}) == []
    assert parse_trades(msg([{"coin": "BTC"}, trade(px="oops")])) == []


def test_the_stream_hands_each_live_trade_to_the_sink_and_survives_junk():
    seen = []
    flow = FuturesFlow(("BTC",), lambda *a: seen.append(a))
    flow.handle("not json")
    flow.handle(json.dumps(msg([trade(), trade(side="A")])))
    assert [(s[0], s[4]) for s in seen] == [("BTC", True), ("BTC", False)]


def test_trades_in_one_second_become_one_row_written_only_once_the_second_is_over(tmp_path):
    tape = Tape(tmp_path / "t.db")
    t0 = 1_800_000_000
    tape.add_fut("BTC", t0 + 0.1, 80000.0, 0.5, True)    # $40,000 bought
    tape.add_fut("BTC", t0 + 0.7, 80010.0, 0.25, False)  # $20,002.5 sold, the last price of the second
    tape.add_fut("ETH", t0 + 0.4, 2400.0, 1.0, True)
    assert tape.flush(now=t0 + 1.5) == 0  # the second is not over yet: nothing written, nothing lost
    assert tape.flush(now=t0 + 3.0) == 2
    rows = {r[1]: tuple(r) for r in tape.db.execute("SELECT ts, symbol, px, buy_usd, sell_usd, buy_n, sell_n FROM fut_1s").fetchall()}
    assert rows["BTC"] == (t0, "BTC", 80010.0, 40000.0, 20002.5, 1, 1)
    assert rows["ETH"] == (t0, "ETH", 2400.0, 2400.0, 0.0, 1, 0)
    assert tape.flush(now=t0 + 10) == 0  # not written twice


class Flaky:
    def __init__(self, real):
        self.real, self.fail = real, True

    def executemany(self, *a, **k):
        if self.fail:
            self.fail = False
            raise RuntimeError("connection lost")
        return self.real.executemany(*a, **k)

    def __getattr__(self, name):
        return getattr(self.real, name)


def test_a_failed_flush_keeps_the_futures_seconds_and_merges_late_trades(tmp_path):
    tape = Tape(tmp_path / "t.db")
    t0 = 1_800_000_000
    tape.add_fut("BTC", t0 + 0.2, 80000.0, 1.0, True)
    real = tape.db
    tape.db = Flaky(real)
    try:
        tape.flush(now=t0 + 5)
        raise AssertionError("the first flush should have failed")
    except RuntimeError:
        pass
    tape.add_fut("BTC", t0 + 0.9, 80005.0, 1.0, True)  # a late trade for the same second
    assert tape.flush(now=t0 + 6) == 1
    row = real.execute("SELECT px, buy_usd, buy_n FROM fut_1s").fetchone()
    assert (row[0], row[1], row[2]) == (80005.0, 160005.0, 2)


def test_the_recorder_is_on_by_default_for_the_five_coins():
    s = config.load()
    assert s.fut_flow_enabled is True and s.fut_flow_coins == ("BTC", "ETH", "SOL", "XRP", "DOGE")
