"""The memory census (memcensus.py; 2026-11-26): a report on a signal, from memory only."""

import collections
import logging

from kalshi_moonshot import memcensus


def test_the_report_names_the_biggest_containers_and_their_contents():
    held = collections.deque((i * 1.0, i * 2.0) for i in range(60000))
    lines = memcensus.report()
    text = "\n".join(lines)
    assert lines[0].startswith("RSS") and "tracked objects" in lines[1]
    assert "type " in text and "with contents" in text and "deque of 60000" in text
    assert held


def test_run_once_logs_one_report(caplog):
    with caplog.at_level(logging.WARNING, logger="moonshot"):
        memcensus.run_once()
    assert "memory census" in caplog.text and "tracked objects" in caplog.text


def test_the_signal_handler_is_installed():
    import signal

    memcensus.start()
    assert signal.getsignal(signal.SIGUSR1) is not signal.SIG_DFL
