"""The perp's chance as a level, against the market's own recent readings (2026-11-21).

The perp_worth step read the perp's chance that a side pays and gated on its GAP to the ask (lane field buy_curve_edge_min). On the bot's own bets the gap did not separate the bets that ended ahead from the ones that ended behind, and the chance itself did (132 live commodity bets with step notes, 11-19 to 11-20, 28 behind: by thirds of the gap 80%, 80%, 77% ahead; by thirds of the chance 68%, 75%, 93%; studies/results/2026-11-21-own-bets-live-commodity-per-bet.txt). A lead on 28 losing bets, not a finding.

So the step can gate on the chance as a level (lane field perp_chance_quantile). The bar is not a fixed number (CLAUDE.md rule 2): it is that quantile of the series' own readings of the last KEEP seconds, the readings the step itself made (one per market side every EVERY seconds), cut again at most every RECUT seconds. With under NEED readings, or readings that do not yet span SPAN seconds, there is no bar and the step says so and falls back to the gap rule.

Memory only, stdlib only: no table, no file. A restart starts the history again (the gap rule answers until it is back).
"""

from __future__ import annotations

import bisect
import threading
from collections import deque

KEEP = 24 * 3600.0  # the market's own recent history: a day of its readings
NEED = 100  # readings before a quantile is read from them
SPAN = 3600.0  # ...and they must reach back at least an hour, so a bar is not cut from one market
EVERY = 10.0  # seconds between two kept readings of one market side (the step is asked on every tick)
RECUT = 60.0  # seconds a cut bar is reused

_lock = threading.Lock()
_hist: dict[str, deque[tuple[float, float]]] = {}
_last: dict[tuple[str, str], float] = {}
_bars: dict[tuple[str, float], tuple[float, float | None, int]] = {}


def clear() -> None:
    with _lock:
        _hist.clear()
        _last.clear()
        _bars.clear()


def note(series: str, ticker: str, side: str, chance: float, now: float) -> None:
    """Keep one reading of a market side, at most every EVERY seconds."""
    with _lock:
        key = (ticker, side)
        if now - _last.get(key, -1e18) < EVERY:
            return
        _last[key] = now
        h = _hist.setdefault(series, deque())
        h.append((now, float(chance)))
        while h and h[0][0] < now - KEEP:
            h.popleft()
        if len(_last) > 4096:  # markets long closed
            for k in [k for k, t in _last.items() if now - t > 3600.0]:
                del _last[k]


def bar(series: str, q: float, now: float) -> tuple[float | None, int]:
    """(the q quantile of the series' own readings of the last KEEP seconds, how many there were), cut at most every RECUT seconds. (None, n) while the history is short."""
    with _lock:
        got = _bars.get((series, q))
        if got is not None and 0.0 <= now - got[0] < RECUT:
            return got[1], got[2]
        h = _hist.get(series)
        rows = [v for t, v in h if now - KEEP <= t < now] if h else []
        value: float | None = None
        if len(rows) >= NEED and h and now - next(t for t, _ in h if t >= now - KEEP) >= SPAN:
            rows.sort()
            value = rows[min(len(rows) - 1, int(len(rows) * q))]
        _bars[(series, q)] = (now, value, len(rows))
        return value, len(rows)


def quantile_bar(times: list[float], values: list[float], q: float, now: float) -> tuple[float | None, int]:
    """The same bar from a recorded history (times ascending), for a study: the q quantile of the readings in [now - KEEP, now)."""
    lo, hi = bisect.bisect_left(times, now - KEEP), bisect.bisect_left(times, now)
    rows = sorted(values[lo:hi])
    if len(rows) < NEED or now - times[lo] < SPAN:
        return None, len(rows)
    return rows[min(len(rows) - 1, int(len(rows) * q))], len(rows)
