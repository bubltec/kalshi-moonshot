"""How many requests the bot makes to Kalshi, and who makes them.

Every Kalshi request is counted by method and path (tickers and ids replaced, so every market's order book is one line) and by source: the kind of job that sent it, or the name of the thread when it is
not in a job (discovery, the watcher, the tape). Once a minute one log line gives the total and the biggest paths and sources, so a rate limit can be spent on what matters.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter
from collections.abc import Callable

log = logging.getLogger("moonshot")

_lock = threading.Lock()
_paths: Counter[str] = Counter()
_sources: Counter[str] = Counter()
_writes = 0
_since = time.time()  # when the counts were last reset
_tl = threading.local()
EVERY = 60.0  # seconds between log lines
TOP = 6  # paths and sources shown
_ID = re.compile(r"^(?:[A-Z0-9]+(?:[-.][A-Za-z0-9.]+)+|[0-9a-f]{8}-[0-9a-f-]{20,}|[0-9a-f]{24,})$")


def shape(path: str) -> str:
    """The path with every ticker or id segment replaced by :id, and no query string."""
    return "/".join(":id" if _ID.match(part) else part for part in path.split("?")[0].split("/"))


def set_source(name: str | None) -> None:
    """Name what this thread is doing until it sets another (a job kind), or None to fall back to the thread's name."""
    _tl.source = name


def count(method: str, path: str) -> None:
    global _writes
    source = getattr(_tl, "source", None) or threading.current_thread().name
    with _lock:
        _paths[f"{method.upper()} {shape(path)}"] += 1
        _sources[source] += 1
        if method.upper() != "GET":
            _writes += 1


def flush() -> str:
    """The line for the requests since the last flush, and reset the counts. '' when there were none."""
    global _writes, _since
    with _lock:
        seconds = max(1.0, time.time() - _since)
        _since = time.time()
        total, writes = sum(_paths.values()), _writes
        paths, sources = _paths.most_common(TOP), _sources.most_common(TOP)
        _paths.clear()
        _sources.clear()
        _writes = 0
    if not total:
        return ""
    return (f"kalshi requests: {total} in the last minute ({total / seconds:.1f}/s; {total - writes} reads, {writes} writes). "
            f"By path: {', '.join(f'{k} {v}' for k, v in paths)}. By source: {', '.join(f'{k} {v}' for k, v in sources)}")


def log_loop(stopped: Callable[[], bool]) -> None:
    """Write the line once a minute until `stopped()`."""
    due = time.time() + EVERY
    while not stopped():
        time.sleep(1.0)
        if time.time() >= due:
            due = time.time() + EVERY
            line = flush()
            if line:
                log.info(line)
