"""Where the tape recorder's memory goes (2026-11-15, user: "fix the leak"). The recorder grew about 70 MiB an hour and was killed once at its 512 MiB limit (05:03 ET); its limit is 1 GiB now. This logs, every few minutes when RECORDER_DIAG is set, the size of every in-memory store it feeds, the object types that are piling up, and (with RECORDER_TRACEMALLOC) the allocation sites that grew since the first snapshot, so the growth can be pinned on a line and not guessed. It changes nothing the recorder does."""

from __future__ import annotations

import gc
import logging
import resource
import tracemalloc
from collections import Counter
from typing import Any

log = logging.getLogger("kalshi_moonshot.recorder")

_baseline: tracemalloc.Snapshot | None = None


def start_tracing(frames: int = 6) -> None:
    """Start tracemalloc and take the baseline the later reports are measured from."""
    global _baseline
    if not tracemalloc.is_tracing():
        tracemalloc.start(frames)
    _baseline = None


def sizes(rec: Any) -> str:
    """One line: how much each in-memory store holds right now."""
    from . import tape as tape_mod
    from . import ticks as ticks_mod

    parts = [f"peak rss {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MiB"]
    mids = ticks_mod.mids
    with mids._lock:
        parts.append(f"mids {len(mids._rows)} tickers {sum(len(r) for r in mids._rows.values())} rows")
    hist = ticks_mod.history
    with hist._lock:
        parts.append(f"history {len(hist._ts)} indexes {sum(len(a) for a in hist._ts.values())} ticks")
    recent = tape_mod.recent
    with recent._lock:
        parts.append(f"recent quotes {len(recent._quotes)} tickers {sum(len(r) for r in recent._quotes.values())} rows, stop {len(recent._stop)}, index {len(recent._index)} {sum(len(v[1]) for v in recent._index.values())} rows, feed {len(recent._feed)}")
    tape = getattr(rec, "tape", None)
    if tape is not None:
        parts.append("writer queues " + " ".join(f"{name} {len(getattr(tape, name, ()) or ())}" for name in ("_quotes", "_index", "_feed", "_flow", "_fast")))
    stream = getattr(rec, "stream", None)
    if stream is not None:
        parts.append(f"stream following {len(getattr(stream, '_tickers', ()) or ())} tickers")
    return " | ".join(parts)


def object_types(top: int = 6) -> str:
    """The object types with the most live instances (a pile of one type points at a container nobody empties)."""
    counts = Counter(type(o).__name__ for o in gc.get_objects())
    return ", ".join(f"{name} {n}" for name, n in counts.most_common(top))


def growth(top: int = 6) -> str:
    """With tracing on: the allocation sites whose memory grew most since the baseline (the first call takes it)."""
    global _baseline
    if not tracemalloc.is_tracing():
        return "tracemalloc off"
    snap = tracemalloc.take_snapshot().filter_traces((tracemalloc.Filter(False, tracemalloc.__file__), tracemalloc.Filter(False, "<frozen importlib._bootstrap>")))
    if _baseline is None:
        _baseline = snap
        cur, _peak = tracemalloc.get_traced_memory()
        return f"baseline taken (traced {cur / 1048576:.0f} MiB)"
    diffs = snap.compare_to(_baseline, "lineno")[:top]
    cur, _peak = tracemalloc.get_traced_memory()
    return f"traced {cur / 1048576:.0f} MiB; grew since the baseline: " + "; ".join(f"{d.size_diff / 1048576:+.1f} MiB {d.traceback[0].filename.split('/')[-1]}:{d.traceback[0].lineno} (+{d.count_diff} blocks)" for d in diffs)


def report(rec: Any) -> str:
    return f"recorder diag: {sizes(rec)} | types: {object_types()} | {growth()}"
