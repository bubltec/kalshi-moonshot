"""Every settlement-index tick of the run, in memory, so a decision never has to ask the database what the spot has been doing.

One pair of float arrays per index (times, values): 16 bytes a tick, so a day of a 1 Hz feed is about 1.4 MB. tape.Recent.note_index feeds it as ticks arrive; warm() fills the hours before this
process started, once, from the tape. A read is two bisects and a slice. Nothing here touches the database wrapper: warm() uses a plain read on the tape, and only at start-up.
"""

from __future__ import annotations

import bisect
import itertools
import logging
import threading
from array import array
from collections import deque
from collections.abc import Sequence

log = logging.getLogger("kalshi_moonshot.ticks")

KEEP_SECONDS = 12 * 3600.0  # how much of each index is kept; older ticks are dropped in chunks
KEEP_BY_PREFIX = {"hl:": 3600.0}  # 2026-11-08: Hyperliquid perp trades (futflow.py) arrive many times a second, so a symbol with this prefix keeps an hour, enough for a rolling typical minute and a spike read
TRIM_SLACK = 1800.0  # trim only when this much extra has piled up, so a trim is rare and cheap per tick


class TickHistory:
    def __init__(self, keep: float = KEEP_SECONDS) -> None:
        self.keep = keep
        self.slack = min(TRIM_SLACK, keep)
        self.keep_by_prefix = dict(KEEP_BY_PREFIX)
        self._lock = threading.Lock()
        self._ts: dict[str, array] = {}
        self._val: dict[str, array] = {}
        self._from: dict[str, float] = {}  # the earliest time this index is known to be complete from (a warm-up start, or the first live tick)

    def note(self, index_id: str, ts: float, value: float) -> None:
        if value <= 0:
            return
        with self._lock:
            t = self._ts.get(index_id)
            if t is None:
                t = self._ts[index_id] = array("d")
                self._val[index_id] = array("d")
                self._from[index_id] = ts
            elif t and ts < t[-1]:
                return  # a late or duplicate tick: the series stays ordered
            t.append(ts)
            self._val[index_id].append(value)
            keep = next((k for p, k in self.keep_by_prefix.items() if index_id.startswith(p)), self.keep)
            if ts - t[0] > keep + min(self.slack, keep):
                cut = bisect.bisect_left(t, ts - keep)
                del t[:cut]
                del self._val[index_id][:cut]
                self._from[index_id] = max(self._from[index_id], t[0])

    def warm(self, index_id: str, rows: Sequence[tuple[float, float]], loaded_from: float) -> None:
        """Put older ticks (ascending, from the tape) in front of whatever has arrived live. Done once per index."""
        with self._lock:
            if index_id in self._from and self._from[index_id] <= loaded_from:
                return
            live_ts, live_val = self._ts.get(index_id), self._val.get(index_id)
            t, v = array("d"), array("d")
            first_live = live_ts[0] if live_ts else float("inf")
            for ts, value in rows:
                if ts < first_live and value > 0:
                    t.append(ts)
                    v.append(value)
            if live_ts is not None and live_val is not None:
                t.extend(live_ts)
                v.extend(live_val)
            self._ts[index_id], self._val[index_id] = t, v
            self._from[index_id] = loaded_from

    def covers(self, index_id: str, start: float) -> bool:
        with self._lock:
            return index_id in self._from and self._from[index_id] <= start and bool(self._ts.get(index_id))

    def window(self, index_id: str, start: float, end: float) -> tuple[list[float], list[float]] | None:
        """(times, values) with start <= ts <= end, or None when the history does not reach `start` (the caller then asks the tape)."""
        with self._lock:
            if index_id not in self._from or self._from[index_id] > start or not self._ts.get(index_id):
                return None
            t, v = self._ts[index_id], self._val[index_id]
            lo, hi = bisect.bisect_left(t, start), bisect.bisect_right(t, end)
            return t[lo:hi].tolist(), v[lo:hi].tolist()

    def first_at_or_after(self, index_id: str, start: float, within: float) -> float | None:
        """The first tick value at or after `start` and no more than `within` seconds later (a window-open reference), or None."""
        with self._lock:
            if index_id not in self._from or self._from[index_id] > start or not self._ts.get(index_id):
                return None
            t = self._ts[index_id]
            i = bisect.bisect_left(t, start)
            if i >= len(t) or t[i] - start > within:
                return None
            return self._val[index_id][i]

    def size(self, index_id: str) -> int:
        with self._lock:
            return len(self._ts.get(index_id, ()))

    def clear(self) -> None:
        with self._lock:
            self._ts.clear()
            self._val.clear()
            self._from.clear()


history = TickHistory()


class MidHistory:
    """The last few minutes of every market's quote mid (yes side), in memory, so a buy can ask how choppy the price has been without a query. Fed by tape.Recent.note_quote."""

    def __init__(self, keep: float = 900.0) -> None:
        self.keep = keep
        self._lock = threading.Lock()
        self._rows: dict[str, deque[tuple[float, float]]] = {}

    def note(self, ticker: str, ts: float, bid: float, ask: float) -> None:
        if bid <= 0 or ask <= 0:
            return
        with self._lock:
            rows = self._rows.get(ticker)
            if rows is None:
                rows = self._rows[ticker] = deque()
            rows.append((ts, (bid + ask) / 2.0))
            cut = ts - self.keep
            while rows and rows[0][0] < cut:
                rows.popleft()

    def crossings(self, ticker: str, now: float, seconds: float = 300.0, level: float = 0.5, step: float = 5.0) -> int | None:
        """How many times the mid crossed `level` in the last `seconds`, on a `step` grid that carries the last quote forward (the way the study counted it). None when the history is too thin to say:
        under 40 of the 60 grid points have a quote within 20 s, or the first quote is later than the start of the span."""
        with self._lock:
            rows = list(self._rows.get(ticker, ()))
        if not rows:
            return None
        times = [r[0] for r in rows]
        if times[0] > now - seconds + 20.0:
            return None
        grid = []
        n = int(seconds / step)
        for i in range(n + 1):
            t = now - seconds + i * step
            k = bisect.bisect_right(times, t) - 1
            if k >= 0 and t - times[k] <= 20.0:
                grid.append(rows[k][1])
        if len(grid) < int(n * 2 / 3):
            return None
        return sum(1 for a, b in itertools.pairwise(grid) if (a - level) * (b - level) < 0)

    def rows(self, ticker: str) -> list[tuple[float, float]]:
        """A copy of the market's (time, YES mid) rows held in memory, oldest first."""
        with self._lock:
            return list(self._rows.get(ticker, ()))

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()


mids = MidHistory()


class Strikes:
    """The strike (floor_strike) of every open market the tape has listed, in memory. A live market's strike is not in the windows table until it settles, so a buy that needs the strike (a crypto variety, the cushion)
    reads it here. Filled by tape.refresh from the 15-minute listings every 20 s; the oldest entries are dropped past `keep`."""

    def __init__(self, keep: int = 1500) -> None:
        self.keep = keep
        self._lock = threading.Lock()
        self._v: dict[str, float] = {}

    def note(self, ticker: str, strike: float) -> None:
        if strike <= 0:
            return
        with self._lock:
            self._v.pop(ticker, None)
            self._v[ticker] = strike
            while len(self._v) > self.keep:
                self._v.pop(next(iter(self._v)))

    def get(self, ticker: str) -> float | None:
        with self._lock:
            return self._v.get(ticker)

    def clear(self) -> None:
        with self._lock:
            self._v.clear()


strikes = Strikes()


def warm_from_tape(s, indexes: Sequence[str], now: float, hours: float = 8.0) -> int:
    """Load the last `hours` of each index from the tape into `history` (8 by default: the volatility regime reads the last 6 hours and 2 minutes, so a 6-hour load would never cover it). Meant for a background thread at start-up; returns how many ticks were loaded."""
    from . import db

    if not db.tape_exists(s.tape_path):
        return 0
    start = now - hours * 3600.0
    tape = db.connect(s.tape_path, "tape", shared=True)
    total = 0
    for index_id in indexes:
        try:
            rows = [(float(ts), float(v)) for ts, v in tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (index_id, start, now))]
        except Exception:
            log.warning("ticks: could not warm %s", index_id, exc_info=True)
            continue
        history.warm(index_id, rows, start)
        total += len(rows)
    return total
