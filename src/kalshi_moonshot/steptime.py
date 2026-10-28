"""Where one job's time goes.

A worker thread calls begin() before a job and summary() after it. In between, named sections (step) and running costs (add: the wait for the shared request budget, the time inside Kalshi requests,
the wait for a database connection, the time inside database statements) are recorded on that thread only. A thread that has not called begin() records nothing, so the hooks cost one attribute
lookup everywhere else. The summary is written to the log for a job that ran long, so a 50-second buy can be read as, say, 35 s waiting for the request budget and 9 s in requests.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

_tl = threading.local()
MIN_SHOWN = 0.3  # seconds: smaller sections and costs are left out of the summary


def begin() -> None:
    _tl.steps = []
    _tl.costs = {}


def end() -> None:
    _tl.steps = None
    _tl.costs = None


def add(name: str, seconds: float) -> None:
    """Add to a running cost on this thread (no-op when no job is being timed)."""
    costs = getattr(_tl, "costs", None)
    if costs is not None:
        costs[name] = costs.get(name, 0.0) + seconds


@contextmanager
def step(name: str) -> Iterator[None]:
    """Time one named section of the job (sections may nest: an outer one includes its inner ones)."""
    started = time.perf_counter()
    try:
        yield
    finally:
        steps = getattr(_tl, "steps", None)
        if steps is not None:
            steps.append((name, time.perf_counter() - started))


def summary() -> str:
    steps = [f"{n} {s:.1f}s" for n, s in (getattr(_tl, "steps", None) or []) if s >= MIN_SHOWN]
    costs = [f"{n} {s:.1f}s" for n, s in sorted((getattr(_tl, "costs", None) or {}).items(), key=lambda kv: -kv[1]) if s >= MIN_SHOWN]
    parts = []
    if steps:
        parts.append("steps: " + ", ".join(steps))
    if costs:
        parts.append("time in: " + ", ".join(costs))
    return "; ".join(parts)
