"""A learned entry gate: trade a part of the 15-minute window only where the record says it has paid.

A fixed "buy in minutes 11-14" is a hardcoded strategy. Here the window is data. Every finished bet of the lanes named in a
lane's `gate_from` (the paper lanes explore the whole window, the live lane exploits) is filed under how far into its window
it was placed. A band is open for the live lane when it has at least `gate_min_n` finished bets and the mean return per
dollar staked, less `gate_z` standard errors, is above zero. A band with too little evidence is closed, not guessed at: the
paper lanes keep entering there, and the band opens once the record supports it.

Markets differ (spreads, how they follow the S&P, how their books are built), so with `gate_by = "series"` each market keeps its own
record per band. A market with few bets is not trusted on its own luck: its mean is blended with the lane's pooled result for that
band, as if it had `gate_pool` extra bets at the pooled average (partial pooling). It earns its own verdict only as its own bets
accumulate; until then it borrows the pooled one, and a market whose results are clearly worse than the pool is closed.
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
    by: dict[tuple[int, int], list[tuple[float, float]]] = {b: [] for b in BANDS}  # (market close, return): bets closing together are one slot
    wins: dict[tuple[int, int], int] = dict.fromkeys(BANDS, 0)
    for t in journal.trades(store):
        if t.lane not in lanes or not t.series.endswith("15M") or not t.close_ts or not t.opened_ts or t.stake <= 0:
            continue
        if t.opened_ts < since:
            continue
        band = band_of(window - (t.close_ts - t.opened_ts) / 60.0)
        if band is not None:
            by[band].append((t.close_ts, t.pnl / t.stake))
            wins[band] += t.won
    out = {}
    for band, rois in by.items():
        n = len(rois)
        mean = sum(r for _, r in rois) / n if n else 0.0
        out[band] = Cell(band, n, mean, journal.cluster_se(rois, mean), wins[band])
    return out


def series_table(
    store: Store, lanes: tuple[str, ...], window: float = 15.0, since: float = 0.0
) -> dict[tuple[str, tuple[int, int]], Cell]:
    """One Cell per (series, minute band), from the same finished bets as table()."""
    by: dict[tuple[str, tuple[int, int]], list[tuple[float, float]]] = {}
    wins: dict[tuple[str, tuple[int, int]], int] = {}
    for t in journal.trades(store):
        if t.lane not in lanes or not t.series.endswith("15M") or not t.close_ts or not t.opened_ts or t.stake <= 0:
            continue
        if t.opened_ts < since:
            continue
        band = band_of(window - (t.close_ts - t.opened_ts) / 60.0)
        if band is not None:
            by.setdefault((t.series, band), []).append((t.close_ts, t.pnl / t.stake))
            wins[(t.series, band)] = wins.get((t.series, band), 0) + t.won
    out = {}
    for key, rois in by.items():
        n = len(rois)
        mean = sum(r for _, r in rois) / n
        out[key] = Cell(key[1], n, mean, journal.cluster_se(rois, mean), wins[key])
    return out


def shrink(own: Cell | None, pooled: Cell, k: float) -> tuple[float, float, int]:
    """(blended mean, its standard error, the market's own bet count): the market's mean pulled toward the pool by k pseudo-bets."""
    n = own.n if own is not None else 0
    mean_own = own.mean_roi if own is not None else 0.0
    mean = (n * mean_own + k * pooled.mean_roi) / (n + k)
    sd = pooled.se * math.sqrt(pooled.n) if pooled.n > 1 and math.isfinite(pooled.se) else float("inf")
    return mean, sd / math.sqrt(n + k), n


def decide_series(own: Cell | None, pooled: Cell | None, series: str, min_n: int, z: float, k: float) -> tuple[bool, str]:
    if pooled is None:
        return False, "learned gate: no entry band for this minute"
    lo, hi = pooled.band
    if pooled.n < min_n:
        return False, f"learned gate: minutes {lo}-{hi} has {pooled.n} finished bets across the lane, needs {min_n}"
    mean, se, n = shrink(own, pooled, k)
    lower = mean - z * se
    text = (f"{series} minutes {lo}-{hi}: {n} own bets, blended return {mean:+.0%} (own {own.mean_roi if own else 0:+.0%}, "
            f"pool {pooled.mean_roi:+.0%} on {pooled.n}), lower bound {lower:+.0%}")
    return (lower > 0), ("learned gate open: " if lower > 0 else "learned gate closed: ") + text


_CACHE: dict[tuple[int, tuple[str, ...], float], tuple[float, dict[tuple[int, int], Cell]]] = {}
_SCACHE: dict[tuple[int, tuple[str, ...], float], tuple[float, dict[tuple[str, tuple[int, int]], Cell]]] = {}


def cached_table(store: Store, lanes: tuple[str, ...], now: float | None = None, since: float = 0.0) -> dict[tuple[int, int], Cell]:
    now = time.time() if now is None else now
    key = (id(store), lanes, since)
    hit = _CACHE.get(key)
    if hit and now - hit[0] < TTL:
        return hit[1]
    t = table(store, lanes, since=since)
    _CACHE[key] = (now, t)
    return t


def cached_series_table(store: Store, lanes: tuple[str, ...], now: float | None = None, since: float = 0.0):
    now = time.time() if now is None else now
    key = (id(store), lanes, since)
    hit = _SCACHE.get(key)
    if hit and now - hit[0] < TTL:
        return hit[1]
    t = series_table(store, lanes, since=since)
    _SCACHE[key] = (now, t)
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


def check(store: Store, spec: LaneSpec, close_time: str, now: float | None = None, series: str = "") -> tuple[bool, str]:
    """Is the lane allowed to enter this market now? Lanes without a gate are always allowed."""
    if not spec.gate_from:
        return True, ""
    now = time.time() if now is None else now
    try:
        close = datetime.fromisoformat(close_time).timestamp()
    except (TypeError, ValueError):
        return False, "learned gate: no close time"
    band = band_of(spec.timed_minutes - (close - now) / 60.0) or (-1, -1)
    since = since_ts(spec.gate_since)
    pooled = cached_table(store, tuple(spec.gate_from), now, since).get(band)
    if spec.gate_by == "series" and series:
        own = cached_series_table(store, tuple(spec.gate_from), now, since).get((series, band))
        return decide_series(own, pooled, series, spec.gate_min_n, spec.gate_z, spec.gate_pool)
    return decide(pooled, spec.gate_min_n, spec.gate_z)
