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


def active() -> bool:
    """Whether this thread is timing a job (so a caller can skip building a label nobody will read)."""
    return getattr(_tl, "costs", None) is not None


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


def breakdown_ms(min_ms: float = 1.0) -> str:
    """The job's steps and running costs in milliseconds (every one of at least `min_ms`), for the per-entry timing line: where the time of one evaluation goes, not only of a slow one."""
    steps = [f"{n} {s * 1000.0:.0f}" for n, s in (getattr(_tl, "steps", None) or []) if s * 1000.0 >= min_ms]
    costs = [f"{n} {s * 1000.0:.0f}" for n, s in sorted((getattr(_tl, "costs", None) or {}).items(), key=lambda kv: -kv[1]) if s * 1000.0 >= min_ms]
    return "; ".join(p for p in (("steps ms: " + ", ".join(steps)) if steps else "", ("time in ms: " + ", ".join(costs)) if costs else "") if p)


# ---- per-tick decision latency (CLAUDE.md, 2026-11-19: "under 60 ms to evaluate per tick") ----
TICK_BUDGET_MS = 60.0  # the rule's own number, structural
_RING = 2000  # readings kept per kind
_lock = threading.Lock()
_ticks: dict[str, list[float]] = {}  # kind -> the last readings, in ms
_counts: dict[str, int] = {}
_over: dict[str, int] = {}
_reported = [0.0]
REPORT_EVERY = 300.0  # seconds between summary lines; the line is written by whichever decision lands after the gap, so no timer thread


def tick_record(kind: str, ms: float) -> None:
    """Record one tick's decision time (kinds: "sell" = a quote tick through the sell logic of every bet on the market, "buy review" = a tick through the entry thread's review, "buy evaluate" = the buy module's steps and the order for one shot)."""
    with _lock:
        rows = _ticks.setdefault(kind, [])
        rows.append(ms)
        del rows[:-_RING]
        _counts[kind] = _counts.get(kind, 0) + 1
        if ms > TICK_BUDGET_MS:
            _over[kind] = _over.get(kind, 0) + 1


@contextmanager
def tick(kind: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        tick_record(kind, (time.perf_counter() - started) * 1000.0)


def tick_stats() -> dict[str, dict[str, float]]:
    """For each kind: n (all time), over (readings above the budget, all time), p50, p95, p99, max of the kept readings, in ms."""
    out: dict[str, dict[str, float]] = {}
    with _lock:
        for kind, rows in _ticks.items():
            if not rows:
                continue
            xs = sorted(rows)
            def q(p: float, xs: list[float] = xs) -> float:
                return xs[min(len(xs) - 1, int(p * len(xs)))]
            out[kind] = {"n": float(_counts.get(kind, 0)), "over": float(_over.get(kind, 0)), "p50": q(0.5), "p95": q(0.95), "p99": q(0.99), "max": xs[-1]}
    return out


def tick_report(now: float | None = None, force: bool = False) -> str | None:
    """A summary line when REPORT_EVERY has passed since the last one (or forced), else None. Called from the decision paths themselves."""
    now = time.time() if now is None else now
    if not force and now - _reported[0] < REPORT_EVERY:
        return None
    _reported[0] = now
    stats = tick_stats()
    if not stats:
        return None
    return f"tick latency (ms, budget {TICK_BUDGET_MS:.0f}): " + "; ".join(
        f"{k} n {int(v['n'])} p50 {v['p50']:.1f} p95 {v['p95']:.1f} p99 {v['p99']:.1f} max {v['max']:.1f} over {int(v['over'])}" for k, v in sorted(stats.items())
    )


def tick_reset() -> None:
    with _lock:
        _ticks.clear()
        _counts.clear()
        _over.clear()
    _reported[0] = 0.0
