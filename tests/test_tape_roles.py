"""The tape recorder as a process of its own (2026-11-14, user: finish the restart fix end to end; the hole in the tape at every restart was the bot's recorder going down with the bot).
Three roles share one setting, TAPE_ROLE: "both" (a process that streams and writes, as before), "memory" (the bot: it keeps the Kalshi streams for its own memory and leaves their database rows to the recorder) and "record" (the recorder process: the Kalshi streams, nothing else).
Unit tests of the tape and the settings, then the recorder object in each role, then a recorder and a bot over one tape."""
import dataclasses
import time

import pytest

from kalshi_moonshot import config
from kalshi_moonshot import ticks as ticks_mod
from kalshi_moonshot.__main__ import COMMANDS, build_parser
from kalshi_moonshot.tape import Tape, TapeRecorder

NOW = time.time()
TICKER = "KXBTC15M-26NOV141830-30"


def quote(ts=NOW, bid=0.50, ask=0.52, src="kalshi"):
    e = {"kind": "quote", "ticker": TICKER, "ts": ts, "bid": bid, "ask": ask, "bid_size": 10, "ask_size": 10}
    if src:
        e["src"] = src
    return e


def count(tape, table):
    return tape.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("TAPE_ROLE", raising=False)

    def wipe():  # the in-memory stores are process-wide: an earlier test's BRTI history made this file's first read of it miss
        ticks_mod.mids._rows.clear()
        for name in ("_ts", "_val", "_from"):
            getattr(ticks_mod.history, name).clear()

    wipe()
    yield
    wipe()


def settings(tmp_path, monkeypatch, role=None):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    if role:
        monkeypatch.setenv("TAPE_ROLE", role)
    return config.load()


# ---- unit ----
def test_unit_the_role_defaults_to_both_and_a_bad_value_is_both(tmp_path, monkeypatch):
    assert settings(tmp_path, monkeypatch).tape_role == "both"
    assert settings(tmp_path, monkeypatch, "memory").tape_role == "memory"
    assert settings(tmp_path, monkeypatch, "RECORD").tape_role == "record"
    assert settings(tmp_path, monkeypatch, "nonsense").tape_role == "both"


def test_unit_an_external_tape_keeps_kalshi_events_out_of_the_database_and_in_memory(tmp_path):
    tape = Tape(tmp_path / "t.db", external=True)
    tape.add(quote())
    tape.add({"kind": "trade", "ticker": TICKER, "ts": NOW, "taker": "yes", "count": 3, "price": 0.5, "src": "kalshi"})
    tape.add({"kind": "index", "index_id": "BRTI", "ts": NOW, "value": 65000.0, "src": "kalshi"})
    tape.add({"kind": "index_fast", "index_id": "BRTI", "ts": NOW, "value": 65000.0, "src": "kalshi"})
    tape.flush(NOW + 5)
    assert [count(tape, t) for t in ("quotes", "flow_1s", "index_ticks", "index_fast")] == [0, 0, 0, 0]
    assert len(ticks_mod.mids.rows(TICKER)) == 1  # the process's own memory still has the quote
    assert ticks_mod.history.window("BRTI", NOW, NOW + 1) is not None  # in the process memory the settlement index is there


def test_unit_an_external_tape_still_writes_what_the_recorder_process_does_not_stream(tmp_path):
    tape = Tape(tmp_path / "t.db", external=True)
    tape.add({"kind": "index", "index_id": "BIQUOTE_GOLD", "ts": NOW, "value": 3900.0})  # BiQuote: no src tag
    tape.add_feed("hl:xyz:GOLD", 3901.0, NOW)
    tape.flush(NOW + 5)
    assert count(tape, "index_ticks") == 1 and count(tape, "feed_ticks") == 1


def test_unit_a_tape_that_is_not_external_writes_everything_as_before(tmp_path):
    tape = Tape(tmp_path / "t.db")
    tape.add(quote())
    tape.add({"kind": "index", "index_id": "BRTI", "ts": NOW, "value": 65000.0, "src": "kalshi"})
    tape.flush(NOW + 5)
    assert count(tape, "quotes") == 1 and count(tape, "index_ticks") == 1


def test_unit_the_record_command_exists():
    assert "record" in COMMANDS
    assert build_parser().parse_args(["record"]).command == "record"


# ---- integration: the recorder object in each role ----
def test_integration_the_recorder_process_streams_from_kalshi_and_runs_no_other_feed(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch, "record")
    monkeypatch.setenv("FUT_FLOW_COINS", "BTC")
    rec = TapeRecorder(object(), s, None)  # type: ignore[arg-type]
    assert rec.role == "record" and rec.tape.external is False
    assert rec.futflow is None and rec.biquote is None and rec.pyth is None
    assert rec.stream.fills is False  # the account's fills are the bot's stream


def test_integration_the_bot_beside_a_recorder_keeps_its_feeds_and_its_fills(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch, "memory")
    rec = TapeRecorder(object(), s, None)  # type: ignore[arg-type]
    assert rec.role == "memory" and rec.tape.external is True and rec.stream.fills is True


def test_integration_a_kalshi_event_is_tagged_on_its_way_in(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch, "memory")
    rec = TapeRecorder(object(), s, None)  # type: ignore[arg-type]
    rec._on_kalshi({"kind": "quote", "ticker": TICKER, "ts": NOW, "bid": 0.5, "ask": 0.52})
    rec.tape.flush(NOW + 5)
    assert count(rec.tape, "quotes") == 0  # tagged src=kalshi, so a memory-role bot does not write it
    assert len(ticks_mod.mids.rows(TICKER)) == 1


def test_integration_the_recorder_process_does_not_prune_and_the_bot_does(tmp_path, monkeypatch):
    pruned = []
    for role, expect in (("record", 0), ("both", 1)):
        d = tmp_path / role
        d.mkdir()
        s = settings(d, monkeypatch, role)
        rec = TapeRecorder(object(), s, None)  # type: ignore[arg-type]
        monkeypatch.setattr(rec.tape, "prune", lambda *a, _r=role, **k: pruned.append(_r))
        rec._stop.set()
        rec._stop.wait = lambda t=None: rec._stop.clear() or True  # one pass of the loop, then stop
        calls = []
        real_is_set = rec._stop.is_set
        rec._stop.is_set = lambda: bool(calls) or calls.append(1) or False  # run the body once
        rec._run()
        assert pruned.count(role) == expect, role


# ---- integration: a recorder and a bot over one tape ----
def test_integration_one_row_per_event_when_the_recorder_and_the_bot_share_a_tape(tmp_path, monkeypatch):
    s_rec = settings(tmp_path, monkeypatch, "record")
    recorder = TapeRecorder(object(), s_rec, None)  # type: ignore[arg-type]
    s_bot = dataclasses.replace(s_rec, tape_role="memory")
    bot = TapeRecorder(object(), s_bot, None)  # type: ignore[arg-type]
    events = [{"kind": "quote", "ticker": TICKER, "ts": NOW + k, "bid": 0.5 + 0.01 * (k % 5), "ask": 0.52 + 0.01 * (k % 5)} for k in range(30)]
    for e in events:  # the same stream, delivered to both processes
        recorder._on_kalshi(dict(e))
        bot._on_kalshi(dict(e))
    recorder.tape.flush(NOW + 60)
    bot.tape.flush(NOW + 60)
    assert count(recorder.tape, "quotes") == 30  # not 60
    assert len(ticks_mod.mids.rows(TICKER)) >= 29  # and the bot has them in its own memory


def test_integration_without_the_split_two_processes_would_write_every_row_twice(tmp_path, monkeypatch):
    """What the roles exist to prevent: the same setup with both processes in the default role."""
    s = settings(tmp_path, monkeypatch, "both")
    a, b = TapeRecorder(object(), s, None), TapeRecorder(object(), s, None)  # type: ignore[arg-type]
    for k in range(10):
        e = {"kind": "quote", "ticker": TICKER, "ts": NOW + k, "bid": 0.5 + 0.01 * k, "ask": 0.52 + 0.01 * k}
        a._on_kalshi(dict(e))
        b._on_kalshi(dict(e))
    a.tape.flush(NOW + 60)
    b.tape.flush(NOW + 60)
    assert count(a.tape, "quotes") == 20


def test_integration_the_bot_alone_in_memory_role_leaves_a_gap_the_recorder_fills(tmp_path, monkeypatch):
    """The point of it: the bot stops for a rebuild; the recorder, in its own process, keeps writing through it."""
    s_rec = settings(tmp_path, monkeypatch, "record")
    recorder = TapeRecorder(object(), s_rec, None)  # type: ignore[arg-type]
    bot = TapeRecorder(object(), dataclasses.replace(s_rec, tape_role="memory"), None)  # type: ignore[arg-type]
    for k in range(0, 20):
        e = {"kind": "quote", "ticker": TICKER, "ts": NOW + k, "bid": 0.5 + 0.01 * (k % 7), "ask": 0.52 + 0.01 * (k % 7)}
        recorder._on_kalshi(dict(e))
        if not 5 <= k < 15:  # the bot is down for ten seconds
            bot._on_kalshi(dict(e))
    recorder.tape.flush(NOW + 60)
    got = sorted(r[0] for r in recorder.tape.db.execute("SELECT ts FROM quotes"))
    assert len(got) == 20 and max(b - a for a, b in zip(got, got[1:], strict=False)) <= 1.0001  # no hole in the tape


# ---- the bot writes again when the recorder container is down ----
def test_integration_a_memory_role_bot_takes_over_writing_when_the_recorder_goes_silent(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch, "memory")
    bot = TapeRecorder(object(), s, None)  # type: ignore[arg-type]
    assert bot.recorder_silent(NOW) is False  # its own stream has delivered nothing yet: no comparison
    bot._on_kalshi({"kind": "index", "index_id": "BRTI", "ts": NOW, "value": 65000.0})
    bot.tape.flush(NOW + 5)
    assert bot.recorder_silent(NOW + 5) is True  # its stream is live and the database has no tick: the recorder is not writing
    other = TapeRecorder(object(), dataclasses.replace(s, tape_role="record"), None)  # type: ignore[arg-type]
    other._on_kalshi({"kind": "index", "index_id": "BRTI", "ts": NOW + 5, "value": 65001.0})
    other.tape.flush(NOW + 10)
    bot._on_kalshi({"kind": "index", "index_id": "BRTI", "ts": NOW + 10, "value": 65002.0})
    assert bot.recorder_silent(NOW + 10) is False  # the recorder wrote a tick 5 s ago


def test_integration_a_recorder_that_is_just_slow_is_not_taken_over(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch, "memory")
    bot = TapeRecorder(object(), s, None)  # type: ignore[arg-type]
    other = TapeRecorder(object(), dataclasses.replace(s, tape_role="record"), None)  # type: ignore[arg-type]
    other._on_kalshi({"kind": "index", "index_id": "BRTI", "ts": NOW - 60, "value": 65000.0})
    other.tape.flush(NOW)
    bot._on_kalshi({"kind": "index", "index_id": "BRTI", "ts": NOW, "value": 65001.0})
    assert bot.recorder_silent(NOW) is False  # 60 s old: under the 120 s limit
    bot._on_kalshi({"kind": "index", "index_id": "BRTI", "ts": NOW + 100, "value": 65002.0})
    assert bot.recorder_silent(NOW + 100) is True  # 160 s old
