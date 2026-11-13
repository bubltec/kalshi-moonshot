"""Prices from Kalshi's own perpetual-futures reference (2026-11-12, user: "It should use Kalshi perp"; the ladder estimator read Kraken for crypto until 10-31, and has priced crypto at the ask since).

Each Kalshi perp carries a reference price, about one reading a second, public, and perpref.py records it on the tape as the index "KP_BTC", "KP_ETH" and so on. This feed reads those recorded ticks, so it opens no connection of its own and asks no one for anything.
Symbols are written "kp:KP_BTC" in instruments.toml. The price is a proxy for what the coin markets settle on (a 60 s average of the coin's own index): on the tape the two sit within about half a basis point, and the pricer learns the remaining offset from settled windows instead of assuming it (CommodityPricer.learn_windows), as it does for Hyperliquid.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .commodities import CommodityError, Series

PREFIX = "kp:"
BARS_TTL = 60.0  # seconds a built set of bars is reused
BAR_SECONDS = 300  # the pricer's volatility reads 5-minute closes, as the Hyperliquid feed gives
BAR_HOURS = 6.0  # 72 five-minute bars


def _closes(rows: list[Any], width: int) -> tuple[list[float], list[float]]:
    """(closes, bar open times) of `width`-second bars from (ts, value) rows in time order: the last value of each bar."""
    last: dict[int, float] = {}
    for ts, value in rows:
        last[int(ts // width) * width] = float(value)
    keys = sorted(last)
    return [last[k] for k in keys], [float(k) for k in keys]


class KalshiPerpFeed:
    def __init__(self, tape: Any = None):
        self._tape = tape  # a tape connection, or None: open the configured one on first use, one per thread
        self._local = threading.local()
        self._bars: dict[str, tuple[float, list[float], list[float]]] = {}
        self._minutes: dict[str, tuple[float, dict[int, float]]] = {}
        self._lock = threading.Lock()

    def _db(self) -> Any:
        if self._tape is not None:
            return self._tape
        if getattr(self._local, "db", None) is None:
            from . import config, db

            self._local.db = db.connect(config.reload().tape_path, "tape", shared=True)
        return self._local.db

    def _rows(self, index_id: str, hours: float) -> list[Any]:
        try:
            return self._db().execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value > 0 ORDER BY ts", (index_id, time.time() - hours * 3600.0)).fetchall()
        except Exception as exc:
            self._local.db = None  # a connection that failed is not reused
            raise CommodityError(f"Kalshi perp ticks could not be read for {index_id}: {exc}") from exc

    def latest(self, symbol: str) -> tuple[float, float] | None:
        """(price, the tick's own time) of the newest recorded reading, or None. One indexed query."""
        index_id = symbol.removeprefix(PREFIX)
        try:
            row = self._db().execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND value > 0 ORDER BY ts DESC LIMIT 1", (index_id,)).fetchone()
        except Exception as exc:
            self._local.db = None
            raise CommodityError(f"Kalshi perp ticks could not be read for {index_id}: {exc}") from exc
        return None if row is None else (float(row[1]), float(row[0]))

    # ---- Feed ----
    def series(self, symbol: str) -> Series:
        index_id = symbol.removeprefix(PREFIX)
        tick = self.latest(symbol)
        if tick is None:
            raise CommodityError(f"no Kalshi perp price recorded for {index_id}")
        with self._lock:
            hit = self._bars.get(index_id)
        if not hit or time.time() - hit[0] >= BARS_TTL:
            closes, times = _closes(self._rows(index_id, BAR_HOURS), BAR_SECONDS)
            hit = (time.time(), closes, times)
            with self._lock:
                self._bars[index_id] = hit
        return Series(last=tick[0], last_ts=tick[1], closes=hit[1], times=hit[2])

    def minute_closes(self, symbol: str, hours: float = 4.0) -> dict[int, float]:
        """Close of each 1-minute bar, keyed by the unix second it opened."""
        index_id = symbol.removeprefix(PREFIX)
        with self._lock:
            hit = self._minutes.get(index_id)
        if hit and time.time() - hit[0] < BARS_TTL:
            return hit[1]
        closes, times = _closes(self._rows(index_id, hours), 60)
        out = {int(t): c for t, c in zip(times, closes, strict=True)}
        with self._lock:
            self._minutes[index_id] = (time.time(), out)
        return out
