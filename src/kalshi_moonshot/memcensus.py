"""A memory census of the running bot, on a signal (2026-11-26: the bot holds 2.3 GB within seconds of a start and the cause was not known; the loaded data is small on paper).

`docker kill -s USR1 moonshot-bot-1` writes one report to the log, from a thread of its own (the bot keeps running; gc.get_objects holds the interpreter for a few seconds, so ask for it when the account is flat). The report answers two questions:

1. Is the memory live objects or fragmentation? RSS before and after `malloc_trim(0)`: a large drop is memory the allocator holds but Python no longer uses (many threads, many arenas), and `MALLOC_ARENA_MAX` is the lever.
2. If it is live objects, which? The object count and shallow size by type, and the biggest containers by size with what they hold and who refers to them.

Stdlib only (the image has no numpy). Reads memory only; nothing is written but the log."""

from __future__ import annotations

import collections
import ctypes
import gc
import logging
import signal
import sys
import threading
import time
from typing import Any

log = logging.getLogger("moonshot")

TOP_TYPES = 14
TOP_CONTAINERS = 18
CONTAINERS = (dict, list, set, collections.deque, bytearray)


def rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


def _trim() -> None:
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception as exc:
        log.warning("memory census: malloc_trim is not available (%r)", exc)


def _what(obj: Any) -> str:
    """One short line saying what a container holds."""
    try:
        if isinstance(obj, dict):
            if "__name__" in obj and "__file__" in obj:
                return f"module {obj['__name__']}"
            k = next(iter(obj), None)
            v = obj.get(k) if k is not None else None
            return f"dict of {len(obj)}: key {type(k).__name__} {str(k)[:40]!r} -> {type(v).__name__}"
        if isinstance(obj, bytearray):
            return f"bytearray of {len(obj)}"
        first = next(iter(obj), None)
        return f"{type(obj).__name__} of {len(obj)}: first {type(first).__name__} {str(first)[:50]!r}"
    except Exception:
        return type(obj).__name__


def _deep(obj: Any, cap: int = 4_000_000) -> float:
    """MB of what a container holds, two levels down (strings, floats and tuples of numbers are not tracked by gc, so the census cannot see them any other way)."""
    total = 0
    items = obj.values() if isinstance(obj, dict) else obj
    if isinstance(obj, dict):
        total += sum(sys.getsizeof(k) for k in list(obj)[:cap])
    for n, e in enumerate(items, 1):
        if n > cap:
            break
        total += sys.getsizeof(e)
        if isinstance(e, tuple | list):
            total += sum(sys.getsizeof(x) for x in e)
        elif isinstance(e, dict):
            total += sum(sys.getsizeof(k) + sys.getsizeof(v) for k, v in e.items())
    return total / 1e6


def _owner(obj: Any) -> str:
    """Who points at this container: the first few referrers, by type and a hint of name."""
    out = []
    try:
        for r in gc.get_referrers(obj)[:4]:
            if isinstance(r, dict):
                out.append(f"dict[{('module ' + r['__name__']) if '__name__' in r and '__file__' in r else str(next(iter(r), ''))[:30]}]")
            elif isinstance(r, type(sys)):
                out.append("module")
            elif hasattr(r, "__class__"):
                out.append(r.__class__.__name__)
    except Exception:
        pass
    return ", ".join(out)


def report() -> list[str]:
    lines = []
    before = rss_mb()
    gc.collect()
    mid = rss_mb()
    _trim()
    after = rss_mb()
    lines.append(f"RSS {before:.0f} MB, after gc.collect {mid:.0f} MB, after malloc_trim {after:.0f} MB (the allocator was holding {mid - after:.0f} MB Python no longer uses)")
    objs = gc.get_objects()
    count: collections.Counter[str] = collections.Counter()
    size: collections.Counter[str] = collections.Counter()
    big: list[tuple[int, Any]] = []
    for o in objs:
        t = type(o).__name__
        try:
            sz = sys.getsizeof(o)
        except Exception:
            continue
        count[t] += 1
        size[t] += sz
        if isinstance(o, CONTAINERS) and sz >= 50_000:
            big.append((sz, o))
    total = sum(size.values())
    lines.append(f"{len(objs):,} tracked objects, {total / 1e6:.0f} MB shallow (gc tracks containers; strings, floats and tuples of numbers are counted only when a container holds them)")
    for t, sz in size.most_common(TOP_TYPES):
        lines.append(f"  type {t:<22} {count[t]:>11,} objects {sz / 1e6:>8.1f} MB")
    big.sort(key=lambda x: -x[0])
    for sz, o in big[:TOP_CONTAINERS]:
        lines.append(f"  container {sz / 1e6:>7.1f} MB shallow, {_deep(o):>8.1f} MB with contents: {_what(o)} | held by {_owner(o)}")
    lines.append(f"threads: {threading.active_count()}")
    return lines


def run_once() -> None:
    t0 = time.time()
    try:
        lines = report()
    except Exception:
        log.exception("memory census failed")
        return
    log.warning("memory census (%.1f s):\n%s", time.time() - t0, "\n".join(lines))


def start() -> None:
    """SIGUSR1 asks for a census; the work runs on its own thread."""
    if not hasattr(signal, "SIGUSR1"):
        return
    signal.signal(signal.SIGUSR1, lambda *_: threading.Thread(target=run_once, name="memory-census", daemon=True).start())
