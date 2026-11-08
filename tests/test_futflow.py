import json

import pytest

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
    assert tape.flush(now=t0 + 1.5) == 3  # the second is not over yet: its row waits (only the three millisecond trades are written), nothing lost
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
    assert tape.flush(now=t0 + 6) == 3  # the merged second row and the two millisecond trades (the first was kept by the failed flush)
    row = real.execute("SELECT px, buy_usd, buy_n FROM fut_1s").fetchone()
    assert (row[0], row[1], row[2]) == (80005.0, 160005.0, 2)


def test_the_recorder_is_on_by_default_for_the_five_coins():
    s = config.load()
    assert s.fut_flow_enabled is True and s.fut_flow_coins == ("BTC", "ETH", "SOL", "XRP", "DOGE")


def test_a_builder_dex_coin_keeps_its_case_and_a_plain_coin_is_upper_cased(monkeypatch):
    monkeypatch.setenv("FUT_FLOW_COINS", "btc, xyz:GOLD,xyz:CL")
    assert config.load().fut_flow_coins == ("BTC", "xyz:GOLD", "xyz:CL")


def test_each_perp_trade_goes_to_memory_at_once_and_a_changed_price_to_the_millisecond_record(tmp_path, monkeypatch):
    from kalshi_moonshot import ticks as ticks_mod

    monkeypatch.setattr(ticks_mod, "history", ticks_mod.TickHistory())
    tape = Tape(tmp_path / "t.db")
    t0 = 1_800_000_000.0
    tape.add_fut("BTC", t0 + 0.101, 80000.0, 0.5, True)
    tape.add_fut("BTC", t0 + 0.215, 80000.0, 0.1, False)  # the same price: in memory, not in the record
    tape.add_fut("BTC", t0 + 0.730, 80010.0, 0.25, False)
    got = ticks_mod.history.window("hl:BTC", t0 + 0.101, t0 + 1)
    assert got is not None and got[0] == [t0 + 0.101, t0 + 0.215, t0 + 0.730] and got[1] == [80000.0, 80000.0, 80010.0]
    assert tape.flush(now=t0 + 5.0) == 3  # two millisecond trades and one second row
    rows = tape.db.execute("SELECT ts, symbol, px, size, bought FROM fut_ticks ORDER BY ts").fetchall()
    assert [tuple(r) for r in rows] == [(t0 + 0.101, "BTC", 80000.0, 0.5, 1), (t0 + 0.730, "BTC", 80010.0, 0.25, 0)]


def test_the_millisecond_record_can_be_switched_off_and_is_capped(tmp_path, monkeypatch):
    from kalshi_moonshot import tape as tape_mod
    from kalshi_moonshot import ticks as ticks_mod

    monkeypatch.setattr(ticks_mod, "history", ticks_mod.TickHistory())
    monkeypatch.setenv("FUT_TICKS", "0")
    off = Tape(tmp_path / "a.db")
    off.add_fut("BTC", 1_800_000_000.1, 80000.0, 0.5, True)
    assert off._fut_ticks == []  # memory still has it
    assert ticks_mod.history.window("hl:BTC", 1_800_000_000.1, 1_800_000_001.0) is not None
    monkeypatch.delenv("FUT_TICKS")
    monkeypatch.setattr(tape_mod, "FUT_TICKS_BUFFER", 3)
    capped = Tape(tmp_path / "b.db")
    for k in range(10):
        capped.add_fut("BTC", 1_800_000_000.0 + k * 0.01, 80000.0 + k, 0.5, True)
    assert len(capped._fut_ticks) == 3  # a stalled database never grows the process


def test_a_failed_flush_keeps_the_millisecond_trades_for_the_next_one(tmp_path, monkeypatch):
    from kalshi_moonshot import ticks as ticks_mod

    monkeypatch.setattr(ticks_mod, "history", ticks_mod.TickHistory())
    tape = Tape(tmp_path / "t.db")
    real = tape.db
    tape.db = Flaky(real)
    tape.db.commit, tape.db.rollback = real.commit, real.rollback
    tape.add_fut("BTC", 1_800_000_000.1, 80000.0, 0.5, True)
    with pytest.raises(RuntimeError):
        tape.flush(now=1_800_000_005.0)
    tape.db = real
    tape.flush(now=1_800_000_006.0)
    assert real.execute("SELECT COUNT(*) FROM fut_ticks").fetchone()[0] == 1


def test_perp_symbols_keep_an_hour_in_memory_and_the_indexes_keep_half_a_day():
    from kalshi_moonshot import ticks as ticks_mod

    h = ticks_mod.TickHistory()
    for k in range(0, 7500, 1):
        h.note("hl:BTC", 1000.0 + k, 80000.0 + k)
        h.note("BRTI", 1000.0 + k, 80000.0 + k)
    hl, idx = h._ts["hl:BTC"], h._ts["BRTI"]
    assert hl[-1] - hl[0] <= 3600.0 + 1800.0 and hl[-1] - hl[0] < 7000  # trimmed to about an hour
    assert idx[-1] - idx[0] == 7499.0  # an index keeps all of it
