"""Memory is filled from the tape before the bot decides anything (2026-11-14, user: "I just want to make sure that's filled when we rebuild or restart"). Until then the reload ran inside a low-priority job and finished 30 to 85 s after the bot was already deciding; nothing waited for it and nothing said how complete it was. Unit tests of the pieces, then the pieces through the real tape and the real evaluate job."""

import logging
import time

import pytest

from kalshi_moonshot import config
from kalshi_moonshot import ticks as ticks_mod
from kalshi_moonshot.tape import Tape

TICKER = "KXBTC15M-30JAN010015-15"


@pytest.fixture(autouse=True)
def _clean():
    ticks_mod._reload_began = 0.0
    ticks_mod._reload_done.clear()
    ticks_mod.mids._rows.clear()
    yield
    ticks_mod._reload_began = 0.0
    ticks_mod._reload_done.clear()
    ticks_mod.mids._rows.clear()


# ---- unit ----
def test_unit_longest_hole_finds_the_longest_stretch_with_no_reading():
    times = [100.0, 101.0, 102.0, 165.0, 166.0]
    assert ticks_mod.longest_hole(times, 100.0, 167.0) == (63.0, 165.0)


def test_unit_the_stretch_from_the_last_reading_to_now_counts():
    assert ticks_mod.longest_hole([100.0, 101.0, 102.0], 100.0, 190.0) == (88.0, 190.0)  # the recorder has been down since 102


def test_unit_a_steady_record_has_no_hole_worth_the_name():
    worst, _ = ticks_mod.longest_hole([float(t) for t in range(100, 200)], 100.0, 200.0)
    assert worst < ticks_mod.HOLE_MIN


def test_unit_buys_wait_only_between_the_start_and_the_end_of_a_reload():
    assert ticks_mod.reload_pending() is False  # no reload was ever started: a test, a script
    ticks_mod.reload_begin(1000.0)
    assert ticks_mod.reload_pending(1001.0) is True
    assert ticks_mod.reload_pending(1000.0 + ticks_mod.RELOAD_LIMIT + 1.0) is False  # a reload that hangs does not stop the bot trading
    ticks_mod._reload_done.set()
    assert ticks_mod.reload_pending(1001.0) is False


# ---- integration: the real tape ----
def taped(tmp_path, monkeypatch, now, hole=(0, 0)):
    """A tape with a quote a second for the ten minutes before `now`, less the seconds in `hole` (counted back from now), and an index tick a second."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    tape = Tape(s.tape_path)
    for back in range(600, 0, -1):
        if hole[0] >= back > hole[1]:
            continue
        ts = now - back
        tape.add({"kind": "quote", "ticker": TICKER, "ts": ts, "bid": 0.50 + (back % 7) * 0.01, "ask": 0.52 + (back % 7) * 0.01})
        tape.add({"kind": "index", "index_id": "BTCUSD_RTI", "ts": ts, "value": 65000.0 + back})
    tape.flush(now)
    ticks_mod.mids._rows.clear()  # the process "restarts": memory is empty, the tape is not
    return s


def test_integration_the_reload_fills_memory_from_the_tape_and_reports_no_hole(tmp_path, monkeypatch, caplog):
    now = time.time()
    s = taped(tmp_path, monkeypatch, now)
    ticks_mod.reload_begin(now)
    with caplog.at_level(logging.INFO, logger="kalshi_moonshot.ticks"):
        report = ticks_mod.reload_from_tape(s, ["BTCUSD_RTI"], now)
    assert len(ticks_mod.mids.rows(TICKER)) >= 590  # the market's last ten minutes are back in memory
    assert report["quotes"] >= 590 and report["ticks"] >= 590 and report["hole"] < ticks_mod.HOLE_MIN
    assert ticks_mod.reload_pending(now + 1.0) is False  # buys are released
    assert any("memory reloaded from the tape" in r.getMessage() and "no hole" in r.getMessage() for r in caplog.records)


def test_integration_a_restart_hole_is_reported_with_its_length(tmp_path, monkeypatch, caplog):
    now = time.time()
    s = taped(tmp_path, monkeypatch, now, hole=(200, 140))  # a minute with no quotes, as a restart leaves
    with caplog.at_level(logging.INFO, logger="kalshi_moonshot.ticks"):
        report = ticks_mod.reload_from_tape(s, ["BTCUSD_RTI"], now)
    assert 59.0 <= report["hole"] <= 62.0
    line = next(r for r in caplog.records if "memory reloaded from the tape" in r.getMessage())
    assert line.levelno == logging.WARNING and "longest hole" in line.getMessage()


def test_integration_with_no_tape_the_reload_still_releases_the_buys(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    ticks_mod.reload_begin()
    ticks_mod.reload_from_tape(s, ["BTCUSD_RTI"])
    assert ticks_mod.reload_pending() is False


# ---- integration: the evaluate job waits ----
def test_integration_no_buy_is_decided_while_memory_is_reloading_and_one_is_after(tmp_path):
    from tests.test_worker import FakeClient, worker

    w = worker(tmp_path, FakeClient())
    w.start()
    w.run_job(w.jobs.claim(kinds={"scan"}))
    job = w.jobs.claim(kinds={"evaluate"})
    before = w.store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    ticks_mod.reload_begin()
    out = w.do_evaluate(job)
    assert "memory is still reloading" in out
    assert w.store.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == before  # nothing decided, nothing bought
    ticks_mod._reload_done.set()
    assert "memory is still reloading" not in w.do_evaluate(job)


def test_integration_a_decided_window_with_no_bid_on_one_side_is_not_a_hole(tmp_path, monkeypatch):
    """The 16:15 restart of 2026-11-14: every coin's window was decided, the yes bid was 0 for its last 50 s, and the first version of the report left those quotes out and called it a hole."""
    now = time.time()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    tape = Tape(s.tape_path)
    for back in range(600, 0, -1):
        decided = 120 >= back > 60  # a minute with the yes side dead: no bid, an ask of a tenth of a cent, the size still changing
        tape.add({"kind": "quote", "ticker": TICKER, "ts": now - back, "bid": 0.0 if decided else 0.50 + (back % 7) * 0.01, "ask": 0.001 if decided else 0.52 + (back % 7) * 0.01, "ask_size": float(back)})
    tape.flush(now)
    report = ticks_mod.reload_from_tape(s, [], now)
    assert report["hole"] < ticks_mod.HOLE_MIN
