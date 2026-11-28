"""Resting buy orders in memory, woken by the websocket (CLAUDE.md, 2026-11-19: "a change in price re-evaluates a resting buy order and may update it").

A resting buy used to be a loop that slept two seconds and asked Kalshi's REST fills list again until the order's time was up, and it never looked at the price again. Now a resting order is registered here when it is sent and the thread that placed it (its market's own thread) waits on it: a streamed fill for its order id and a streamed quote on its market wake it at once. A fill that completes the order ends the wait; a quote re-judges the order (runner._rest_follow, restcheck.py: the lane's buy module asked again, then the order kept, moved either way, or withdrawn). A paper resting buy waits here the same way (runner._paper_rest_wait) under an id of its own that no fill names. Kalshi's own expiration cancels the order at its time, as before. When the socket is not feeding (`live` is False), the old two-second poll runs.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_by_ticker: dict[str, list[Resting]] = {}
_by_order: dict[str, Resting] = {}
_last_event = [0.0]
LIVE_WITHIN = 30.0  # seconds: the socket counts as feeding while some event has arrived this recently (structural)


class Resting:
    def __init__(self, ticker: str, count: float):
        self.ticker = ticker
        self.count = count
        self.filled = 0.0  # contracts the socket has reported filled, over every order id this buy used
        self.quote: tuple[float, float, float] | None = None  # (YES bid, YES ask, time) of the latest streamed quote
        self.version = 0
        self.cond = threading.Condition()
        self.order_ids: set[str] = set()

    def add_order(self, order_id: str) -> None:
        with _lock:
            self.order_ids.add(order_id)
            _by_order[order_id] = self

    def bump(self) -> None:
        with self.cond:
            self.version += 1
            self.cond.notify_all()

    def wait(self, seen: int, timeout: float) -> int:
        """Block until something changed since `seen` (a fill or a quote) or `timeout` seconds pass; returns the version now."""
        with self.cond:
            self.cond.wait_for(lambda: self.version != seen, timeout=max(0.0, timeout))
            return self.version


def open(ticker: str, count: float, order_id: str) -> Resting:
    r = Resting(ticker, count)
    with _lock:
        _by_ticker.setdefault(ticker, []).append(r)
    r.add_order(order_id)
    return r


def close(r: Resting) -> None:
    with _lock:
        rows = _by_ticker.get(r.ticker, [])
        if r in rows:
            rows.remove(r)
        if not rows:
            _by_ticker.pop(r.ticker, None)
        for oid in r.order_ids:
            _by_order.pop(oid, None)


def on_event(e: dict[str, Any]) -> None:
    """The socket's listener: cheap, never raises."""
    kind = e.get("kind")
    if kind not in ("quote", "fill"):
        return
    _last_event[0] = time.time()
    if kind == "fill":
        r = _by_order.get(str(e.get("order_id") or ""))
        if r is not None and e.get("action") in ("buy", ""):
            r.filled += float(e.get("count") or 0.0)
            r.bump()
        return
    for r in list(_by_ticker.get(str(e.get("ticker")), ())):
        if e.get("bid") is not None and e.get("ask") is not None:
            r.quote = (float(e["bid"]), float(e["ask"]), float(e.get("ts") or time.time()))
        r.bump()


def live(client: Any, now: float | None = None) -> bool:
    """Whether the socket is feeding fills and quotes now (the client's lifecycle subscription is up and an event arrived lately)."""
    now = time.time() if now is None else now
    cache = getattr(client, "cache", None)
    return bool(cache is not None and hasattr(cache, "fed") and cache.fed() and now - _last_event[0] <= LIVE_WITHIN)


_missed: dict[tuple[str, str], float] = {}  # (ticker, side) -> when a resting buy of it rested its time and filled nothing (2026-11-28)


def note_miss(ticker: str, side: str, now: float | None = None) -> None:
    """A resting buy of this market and side rested its whole time and filled nothing: the moment it was placed for has passed (2026-11-28, user, on XRP 07:15 NO: the first order at 07:05:12 missed, a second went in 15 s later mid-rise and filled: "we should cancel the order instead of it misses")."""
    with _lock:
        if len(_missed) > 2000:
            _missed.clear()
        _missed[(ticker, side)] = time.time() if now is None else now


def missed_since(ticker: str, side: str, since: float) -> bool:
    """True when a resting buy of this market and side missed at or after `since` (memory only)."""
    with _lock:
        return _missed.get((ticker, side), 0.0) >= since


def reset() -> None:
    with _lock:
        _by_ticker.clear()
        _by_order.clear()
        _missed.clear()
    _last_event[0] = 0.0
