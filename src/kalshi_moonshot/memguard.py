"""A guard on the buy and sell decision: no database read and no REST read inside it.

CLAUDE.md, 2026-11-19: "db calls should not exist on the buy or sell logic, those should only occur in background jobs"; "no db calls should happen and all calculations should come from in-memory store". A decision runs inside `with memguard.decision("buy"):`. Every SELECT sent to the database and every GET sent to Kalshi on that thread while it runs is a breach. A breach is

  - counted and logged once a minute per statement in the running bot (never raised: a read left behind must not stop a live order, it must show up in the log with the statement that made it), and
  - raised as MemoryRuleBroken when `raising` is set (the tests, tests/test_architecture_rules.py), so the build fails on it.

Writes are not breaches: they are queued behind the decision (and the decision's own order is a POST). The guard costs one attribute lookup on a thread that is not inside a decision.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager

log = logging.getLogger("moonshot")
_tl = threading.local()
raising = os.environ.get("MEMGUARD_RAISE") == "1"  # tests set it: a breach raises
breaches: Counter[str] = Counter()  # "buy: SELECT ..." -> how many
_logged: dict[str, float] = {}


class MemoryRuleBroken(RuntimeError):
    pass


@contextmanager
def decision(kind: str) -> Iterator[None]:
    """Mark this thread as inside a buy or sell decision."""
    prev = getattr(_tl, "kind", None)
    _tl.kind = kind
    try:
        yield
    finally:
        _tl.kind = prev


@contextmanager
def allowed() -> Iterator[None]:
    """A read that is not part of the judgement and is allowed to touch the database or Kalshi (the memory feeds fill themselves from here when memory is cold)."""
    prev = getattr(_tl, "kind", None)
    _tl.kind = None
    try:
        yield
    finally:
        _tl.kind = prev


def note(what: str, sql: str) -> None:
    """Called by the database layer for each statement and by the Kalshi client for each request."""
    kind = getattr(_tl, "kind", None)
    if kind is None:
        return
    head = sql.lstrip()[:7].upper()
    if what == "db" and not head.startswith(("SELECT", "WITH", "PRAGMA")):
        return  # a write is queued or happens after the judgement
    label = f"{kind}: {what} {' '.join(sql.split())[:80]}"
    breaches[label] += 1
    if raising:
        raise MemoryRuleBroken(label)
    now = time.time()
    if now - _logged.get(label, 0.0) >= 60.0:
        _logged[label] = now
        log.warning("memory rule: %s (%d so far)", label, breaches[label])


def reset() -> None:
    breaches.clear()
    _logged.clear()
