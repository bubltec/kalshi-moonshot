"""A learned entry gate: trade a part of the 15-minute window only where the record says it has paid.

A fixed "buy in minutes 11-14" is a hardcoded strategy. Here the window is data. Every finished bet of the lanes named in a
lane's `gate_from` (the paper lanes explore the whole window, the live lane exploits) is filed under how far into its window
it was placed. A band is open for the live lane when it has at least `gate_min_n` finished bets and the mean return per
dollar staked, less `gate_z` standard errors, is above zero. A band with too little evidence is closed, not guessed at: the
paper lanes keep entering there, and the band opens once the record supports it.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from . import journal
from .lanes import LaneSpec
from .store import Store

BANDS = journal.ENTRY_BANDS  # minutes into the window: 0-3, 3-6, 6-9, 9-12, 12-15
TTL = 300.0  # seconds a table is reused


@dataclass(frozen=True)
class Cell:
    band: tuple[int, int]
    n: int
    mean_roi: float
    se: float
    wins: int

    def lower(self, z: float) -> float:
        return self.mean_roi - z * self.se


def band_of(minutes_in: float) -> tuple[int, int] | None:
    for lo, hi in BANDS:
        if lo <= minutes_in < hi:
            return (lo, hi)
    return None


def since_ts(text: str) -> float:
    """Unix time of midnight New York on a YYYY-MM-DD date; 0 for none or a bad value."""
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=ZoneInfo("America/New_York")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def table(store: Store, lanes: tuple[str, ...], window: float = 15.0, since: float = 0.0) -> dict[tuple[int, int], Cell]:
    by: dict[tuple[int, int], list[float]] = {b: [] for b in BANDS}
    wins: dict[tuple[int, int], int] = dict.fromkeys(BANDS, 0)
    for t in journal.trades(store):
        if t.lane not in lanes or not t.series.endswith("15M") or not t.close_ts or not t.opened_ts or t.stake <= 0:
            continue
        if t.opened_ts < since:
            continue
        band = band_of(window - (t.close_ts - t.opened_ts) / 60.0)
        if band is not None:
            by[band].append(t.pnl / t.stake)
            wins[band] += t.won
    out = {}
    for band, rois in by.items():
        n = len(rois)
        mean = sum(rois) / n if n else 0.0
        se = (math.sqrt(sum((r - mean) ** 2 for r in rois) / (n - 1)) / math.sqrt(n)) if n > 1 else float("inf")
        out[band] = Cell(band, n, mean, se, wins[band])
    return out


_CACHE: dict[tuple[int, tuple[str, ...], float], tuple[float, dict[tuple[int, int], Cell]]] = {}


def cached_table(store: Store, lanes: tuple[str, ...], now: float | None = None, since: float = 0.0) -> dict[tuple[int, int], Cell]:
    now = time.time() if now is None else now
    key = (id(store), lanes, since)
    hit = _CACHE.get(key)
    if hit and now - hit[0] < TTL:
        return hit[1]
    t = table(store, lanes, since=since)
    _CACHE[key] = (now, t)
    return t


def decide(cell: Cell | None, min_n: int, z: float) -> tuple[bool, str]:
    if cell is None:
        return False, "learned gate: no entry band for this minute"
    lo, hi = cell.band
    if cell.n < min_n:
        return False, f"learned gate: minutes {lo}-{hi} has {cell.n} finished bets, needs {min_n}"
    lower = cell.lower(z)
    text = f"minutes {lo}-{hi}: {cell.n} bets, mean return {cell.mean_roi:+.0%}, lower bound {lower:+.0%}"
    if lower > 0:
        return True, "learned gate open: " + text
    return False, "learned gate closed: " + text


def check(store: Store, spec: LaneSpec, close_time: str, now: float | None = None) -> tuple[bool, str]:
    """Is the lane allowed to enter this market now? Lanes without a gate are always allowed."""
    if not spec.gate_from:
        return True, ""
    now = time.time() if now is None else now
    try:
        close = datetime.fromisoformat(close_time).timestamp()
    except (TypeError, ValueError):
        return False, "learned gate: no close time"
    cell = cached_table(store, tuple(spec.gate_from), now, since_ts(spec.gate_since)).get(band_of(spec.timed_minutes - (close - now) / 60.0) or (-1, -1))
    return decide(cell, spec.gate_min_n, spec.gate_z)
