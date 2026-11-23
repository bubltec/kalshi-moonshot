"""Memory is filled from the tape before the bot decides anything (2026-11-14, user: "I just want to make sure that's filled when we rebuild or restart"). Until then the reload ran inside a low-priority job and finished 30 to 85 s after the bot was already deciding; nothing waited for it and nothing said how complete it was. Unit tests of the pieces, then the pieces through the real tape and the real evaluate job."""

import datetime
import logging
import time

import pytest

from kalshi_moonshot import config
from kalshi_moonshot import ticks as ticks_mod
from kalshi_moonshot.tape import Tape



TICKER = ""


def current(s, now):
    """The name of the BTC 15-minute market open at `now`, as the tape would hold it."""
    return next(t for t in ticks_mod.recent_15m_tickers(s, now, 0.0) if t.startswith("KXBTC15M-"))


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
    global TICKER
    TICKER = current(s, now)
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
    global TICKER
    TICKER = current(s, now)
    tape = Tape(s.tape_path)
    for back in range(600, 0, -1):
        decided = 120 >= back > 60  # a minute with the yes side dead: no bid, an ask of a tenth of a cent, the size still changing
        tape.add({"kind": "quote", "ticker": TICKER, "ts": now - back, "bid": 0.0 if decided else 0.50 + (back % 7) * 0.01, "ask": 0.001 if decided else 0.52 + (back % 7) * 0.01, "ask_size": float(back)})
    tape.flush(now)
    report = ticks_mod.reload_from_tape(s, [], now)
    assert report["hole"] < ticks_mod.HOLE_MIN


# ---- two stages (2026-11-14, user: "yes do the restart fix first"): the decisions' hour first and the buys released, the rest of the 8 hours behind ----
def long_tape(tmp_path, monkeypatch, now, hours=3.0):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    tape = Tape(s.tape_path)
    n = int(hours * 3600 // 10)
    for k in range(n, 0, -1):  # an index tick every 10 s
        tape.add({"kind": "index", "index_id": "BTCUSD_RTI", "ts": now - k * 10.0, "value": 65000.0 + k})
    tape.flush(now)
    ticks_mod.history._ts.clear()
    ticks_mod.history._val.clear()
    ticks_mod.history._from.clear()
    return s, n


def test_integration_the_buys_are_released_before_the_older_ticks_are_loaded(tmp_path, monkeypatch):
    now = time.time()
    s, _ = long_tape(tmp_path, monkeypatch, now)
    seen = []
    real = ticks_mod.warm_from_tape

    def spy(s_, indexes, now_, hours=8.0, until=None):
        seen.append((hours, until is not None, ticks_mod.reload_pending(now_ + 1.0)))
        return real(s_, indexes, now_, hours=hours, until=until)

    monkeypatch.setattr(ticks_mod, "warm_from_tape", spy)
    ticks_mod.reload_begin(now)
    ticks_mod.reload_from_tape(s, ["BTCUSD_RTI"], now)
    assert seen[0] == (ticks_mod.FIRST_STAGE_HOURS, False, True)  # stage 1: the buys are still waiting
    assert seen[1] == (8.0, True, False)  # stage 2: they have been released, and only the older ticks are asked for


def test_integration_the_two_stages_together_hold_every_tick_once_and_in_order(tmp_path, monkeypatch):
    now = time.time()
    s, n = long_tape(tmp_path, monkeypatch, now, hours=3.0)
    report = ticks_mod.reload_from_tape(s, ["BTCUSD_RTI"], now)
    got = ticks_mod.history.window("BTCUSD_RTI", now - 8 * 3600.0, now)
    assert got is not None
    ts, vs = got
    assert len(ts) == n == report["ticks"]  # no tick twice, none missing
    assert all(a < b for a, b in zip(ts, ts[1:], strict=False))
    assert ts[0] <= now - 3 * 3600.0 + 15.0 and ts[-1] >= now - 15.0


def test_integration_after_stage_one_alone_the_decision_path_has_its_hour(tmp_path, monkeypatch):
    now = time.time()
    s, n = long_tape(tmp_path, monkeypatch, now, hours=3.0)
    got = ticks_mod.warm_from_tape(s, ["BTCUSD_RTI"], now, hours=ticks_mod.FIRST_STAGE_HOURS)
    assert 0 < got < n  # a part of the tape, not all of it
    hour = ticks_mod.history.window("BTCUSD_RTI", now - 3700.0, now)
    assert hour is not None and len(hour[0]) >= 360  # the last hour is there: the width and the exit's long layer read from it
    assert ticks_mod.history.window("BTCUSD_RTI", now - 3 * 3600.0, now) is None  # a reader that wants more asks the tape, as before


def test_integration_a_failed_second_stage_does_not_stop_the_bot(tmp_path, monkeypatch):
    now = time.time()
    s, _ = long_tape(tmp_path, monkeypatch, now)
    real = ticks_mod.warm_from_tape

    def flaky(s_, indexes, now_, hours=8.0, until=None):
        if until is not None:
            raise RuntimeError("the tape went away")
        return real(s_, indexes, now_, hours=hours, until=until)

    monkeypatch.setattr(ticks_mod, "warm_from_tape", flaky)
    ticks_mod.reload_begin(now)
    report = ticks_mod.reload_from_tape(s, ["BTCUSD_RTI"], now)
    assert report["ticks"] > 0 and ticks_mod.reload_pending(now + 1.0) is False



def test_unit_the_tickers_of_the_open_windows_are_built_from_the_series_and_the_quarter_hours(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    s = config.load()
    # 2026-11-14 18:31 ET is 23:31 UTC: the windows of the last hour closed at 17:45 to 18:30, and the one closing at 18:45 is open
    now = datetime.datetime(2026, 11, 14, 23, 31, 0, tzinfo=datetime.UTC).timestamp()
    got = ticks_mod.recent_15m_tickers(s, now, 1.0)
    for name in ("KXBTC15M-26NOV141745-45", "KXBTC15M-26NOV141800-00", "KXETH15M-26NOV141830-30", "KXXRP15M-26NOV141845-45", "KXBTC15M-26NOV141900-00"):
        assert name in got, name  # the names seen on the host, from the 17:45 window to the one after the open one
    assert "KXBTC15M-26NOV141645-45" not in got  # older than the hour
    assert len(got) == len(set(got))


def test_integration_only_the_open_windows_are_read_and_a_market_that_is_not_one_is_ignored(tmp_path, monkeypatch):
    now = time.time()
    s = taped(tmp_path, monkeypatch, now)
    tape = Tape(s.tape_path)
    tape.add({"kind": "quote", "ticker": "KXBTC15M-26JAN010015-15", "ts": now - 30, "bid": 0.5, "ask": 0.52})  # a window from long ago: not in the hour
    tape.add({"kind": "quote", "ticker": "KXSOMETHINGELSE-X-1", "ts": now - 30, "bid": 0.5, "ask": 0.52})
    tape.flush(now)
    ticks_mod.mids._rows.clear()
    ticks_mod.reload_from_tape(s, [], now)
    assert set(ticks_mod.mids._rows) == {TICKER}
