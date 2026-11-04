"""How wild the market is right now, for each coin, against its own normal.

A coin's volatility is the standard deviation of its settlement index's one-minute log returns over the last 6 hours, as an hourly figure in basis points. Its normal is the median of that figure sampled every 30 minutes over
the last 72 hours. The ratio of the two is the regime: over 2026-10-28 to 11-04 the weekend ran at 0.56-0.68 and every weekday since at 1.1-1.8, favorites won 87% in the calm third and 81% in the wild third, and in the wild
regime the underdog of a favorite priced 0.82 or more beat its price (scripts/crypto_vol_regime_study.py).

The numbers are made in a background pass (update) and a buy only reads two dictionary entries: nothing here is a query on the buy path. The pass reads the in-memory tick history for the current figure and the tape for the 72-hour
baseline, which it redoes every few hours.
"""

from __future__ import annotations

import itertools
import logging
import math
import statistics
import threading
import time

from . import ticks as ticks_mod

log = logging.getLogger("kalshi_moonshot.volregime")

LOOK = 6 * 3600.0
BASE_LOOK = 72 * 3600.0
BASE_EVERY = 3 * 3600.0
BASE_STEP = 1800.0
MIN_RETURNS = 200  # of the 360 one-minute returns in 6 hours: fewer than this and the figure is unknown


def hourly_vol_bp(values_by_minute: dict[int, float], end_minute: int) -> float | None:
    """Hourly volatility in bp from the last-tick-of-each-minute values over the 6 hours ending at `end_minute`, or None when there are too few returns."""
    pts = [values_by_minute.get(k) for k in range(end_minute - 360, end_minute + 1)]
    rets = [math.log(b / a) for a, b in itertools.pairwise(pts) if a and b]
    if len(rets) < MIN_RETURNS:
        return None
    return statistics.pstdev(rets) * math.sqrt(60) * 1e4


def minute_values(times: list[float], values: list[float]) -> dict[int, float]:
    out: dict[int, float] = {}
    for t, v in zip(times, values, strict=False):
        out[int(t // 60)] = v
    return out


class VolRegime:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._now: dict[str, float] = {}
        self._base: dict[str, float] = {}
        self._base_at = 0.0
        self.updated_at = 0.0

    def rel(self, coin: str) -> float | None:
        """This coin's volatility over its normal, or None when either is unknown. Reads memory only."""
        with self._lock:
            now, base = self._now.get(coin), self._base.get(coin)
        return now / base if now and base else None

    def set(self, coin: str, now_vol: float | None, base_vol: float | None) -> None:
        with self._lock:
            if now_vol:
                self._now[coin] = now_vol
            if base_vol:
                self._base[coin] = base_vol
            self.updated_at = time.time()

    def snapshot(self) -> dict[str, tuple[float, float]]:
        with self._lock:
            return {c: (v, self._base.get(c, 0.0)) for c, v in self._now.items()}

    def update(self, s, coins: dict[str, str], now: float | None = None) -> dict[str, float | None]:
        """Redo the current figure of every coin ({coin: tape index id}) from the tick history, and the baseline from the tape when it is old. Returns {coin: ratio}."""
        now = time.time() if now is None else now
        for coin, index in coins.items():
            got = ticks_mod.history.window(index, now - LOOK - 120.0, now)
            if got is not None:
                self.set(coin, hourly_vol_bp(minute_values(got[0], got[1]), int(now // 60)), None)
        if now - self._base_at >= BASE_EVERY:
            self._base_at = now
            self._baseline(s, coins, now)
        return {c: self.rel(c) for c in coins}

    def _baseline(self, s, coins: dict[str, str], now: float) -> None:
        from . import db

        if not db.tape_exists(s.tape_path):
            return
        tape = db.connect(s.tape_path, "tape", shared=True)
        for coin, index in coins.items():
            try:
                rows = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (index, now - BASE_LOOK - LOOK, now)).fetchall()
            except Exception:
                log.warning("volregime: could not read %s for its baseline", index, exc_info=True)
                continue
            by_min = minute_values([float(r[0]) for r in rows], [float(r[1]) for r in rows])
            samples = []
            t = now - BASE_LOOK
            while t <= now:
                v = hourly_vol_bp(by_min, int(t // 60))
                if v:
                    samples.append(v)
                t += BASE_STEP
            if len(samples) >= 12:
                self.set(coin, None, statistics.median(samples))
                log.info("volregime: %s normal hourly vol %.1f bp from %d samples", coin, statistics.median(samples), len(samples))


regime = VolRegime()
