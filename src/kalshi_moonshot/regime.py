"""How the favorites have been doing lately, read from the tape and the settled windows. A lane's choppy-market rules key on it.

The favorite of a settled window is the side the crypto favorite lanes would have bought: the first moment from 480 to 120 seconds left when a side's ask is in
0.78-0.92 with a spread of 3c or under. It "won" when that side settled the window. The hit rate over the last few hours is the regime: the same rules won 88-92%
across 2026-11-01/02 mornings and 74-80% on the afternoons that followed. Only windows already settled count, so a regime read never uses the future.
"""

from __future__ import annotations

import logging
import threading
import time

from . import db
from .config import Settings

BAND = (0.78, 0.92)
MAX_SPREAD = 0.03
LEFT = (480.0, 120.0)  # seconds left: a favorite is looked for from the first to the second
MIN_WINDOWS = 12  # fewer settled favorites than this and the regime is unknown (not choppy)
_won: dict[str, bool | None] = {}  # ticker -> whether its favorite won (None: no favorite was on offer)
_rates: dict[tuple, tuple[float, tuple[float | None, int]]] = {}
CACHE_SECONDS = 120.0
MAX_STALE_SECONDS = 900.0  # a remembered rate older than this is computed in line again (the refresh thread must have failed)
log = logging.getLogger("kalshi_moonshot.regime")


def favorite_won(s: Settings, ticker: str, close_ts: float, result: str) -> bool | None:
    if ticker in _won:
        return _won[ticker]
    tape = db.connect(s.tape_path, "tape", shared=True)
    rows = tape.execute(
        "SELECT bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",
        (ticker, close_ts - LEFT[0], close_ts - LEFT[1]),
    ).fetchall()
    out: bool | None = None
    for r in rows:
        yes_bid, yes_ask = float(r[0]), float(r[1])
        for side, bid, ask in (("yes", yes_bid, yes_ask), ("no", 1.0 - yes_ask, 1.0 - yes_bid)):
            if BAND[0] <= ask <= BAND[1] and ask - bid <= MAX_SPREAD + 1e-9:
                out = side == result
                break
        if out is not None:
            break
    _won[ticker] = out
    return out


def _compute_rate(s: Settings, store, series: tuple[str, ...], hours: float, now: float) -> tuple[float | None, int]:
    rows = store.db.execute(
        "SELECT ticker, series, close_ts, result FROM windows WHERE close_ts BETWEEN ? AND ? AND result IN ('yes','no')",
        (now - hours * 3600.0, now),
    ).fetchall()
    hits: list[bool] = []
    for r in rows:
        if r["series"] not in series:
            continue
        w = favorite_won(s, r["ticker"], r["close_ts"], r["result"])
        if w is not None:
            hits.append(w)
    return (sum(hits) / len(hits) if len(hits) >= MIN_WINDOWS else None, len(hits))


_refreshing: set[tuple] = set()
_refresh_lock = threading.Lock()


def favorite_rate(s: Settings, store, series: tuple[str, ...], hours: float, now: float | None = None) -> tuple[float | None, int]:
    """(hit rate, windows counted) of the favorites of `series` that settled in the last `hours`; the rate is None under MIN_WINDOWS windows.

    A buy asks this on its way to the order, so it never waits for the queries: a remembered answer is returned at once, and when it is older than CACHE_SECONDS a background thread recomputes it for the next ask.
    Only the very first ask, with nothing remembered, computes in line. A caller that passes `now` (a replay at a past time) always computes, in line."""
    key = (series, hours)
    hit = _rates.get(key)
    if now is not None:
        return _compute_rate(s, store, series, hours, now)
    now = time.time()
    if hit is not None and now - hit[0] < MAX_STALE_SECONDS:
        if now - hit[0] >= CACHE_SECONDS:
            with _refresh_lock:
                start = key not in _refreshing
                _refreshing.add(key)
            if start:
                threading.Thread(target=_refresh, args=(s, store, series, hours, key), daemon=True, name="regime-refresh").start()
        return hit[1]
    out = _compute_rate(s, store, series, hours, now)
    _rates[key] = (now, out)
    return out


def _refresh(s: Settings, store, series: tuple[str, ...], hours: float, key: tuple) -> None:
    try:
        now = time.time()
        _rates[key] = (now, _compute_rate(s, store, series, hours, now))
    except Exception:
        log.warning("regime: refreshing the favorite rate failed", exc_info=True)
    finally:
        with _refresh_lock:
            _refreshing.discard(key)


def choppy(s: Settings, store, series: tuple[str, ...], hours: float, floor: float, now: float | None = None) -> tuple[bool, float | None, int]:
    """Whether the recent favorite hit rate is under `floor` (an unknown regime is not choppy), with the rate and the windows behind it."""
    rate, n = favorite_rate(s, store, series, hours, now)
    return (rate is not None and rate < floor), rate, n


def reset() -> None:
    _won.clear()
    _rates.clear()
