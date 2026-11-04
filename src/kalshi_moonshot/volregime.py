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
MIN_SHARE = 0.55  # of the one-minute returns in the look-back: fewer than this and the figure is unknown
GLITCH = 0.05  # a one-minute move over 5% is a bad tick, not volatility


def hourly_vol_bp(values_by_minute: dict[int, float], end_minute: int, look: float = LOOK) -> float | None:
    """Hourly volatility in bp from the last-tick-of-each-minute values over the `look` seconds ending at `end_minute`, or None when there are too few returns."""
    n = int(look // 60)
    pts = [values_by_minute.get(k) for k in range(end_minute - n, end_minute + 1)]
    rets = [math.log(b / a) for a, b in itertools.pairwise(pts) if a and b and abs(math.log(b / a)) < GLITCH]
    if len(rets) < MIN_SHARE * n:
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

    def base(self, coin: str) -> float | None:
        with self._lock:
            return self._base.get(coin)

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

    def update(self, s, coins: dict[str, str | tuple[str, float]], now: float | None = None) -> dict[str, float | None]:
        """Redo the current figure of every market ({name: tape index id, or (index id, look-back seconds)}) from the tick history, and the baseline from the tape when it is old. Returns {name: ratio}."""
        now = time.time() if now is None else now
        spec = {c: (v if isinstance(v, tuple) else (v, LOOK)) for c, v in coins.items()}
        for coin, (index, look) in spec.items():
            got = ticks_mod.history.window(index, now - look - 120.0, now)
            if got is not None:
                self.set(coin, hourly_vol_bp(minute_values(got[0], got[1]), int(now // 60), look), None)
        if now - self._base_at >= BASE_EVERY:
            self._base_at = now
            self._baseline(s, spec, now)
        return {c: self.rel(c) for c in coins}

    def _baseline(self, s, spec: dict[str, tuple[str, float]], now: float) -> None:
        from . import db

        if not db.tape_exists(s.tape_path):
            return
        tape = db.connect(s.tape_path, "tape", shared=True)
        for coin, (index, look) in spec.items():
            try:
                rows = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (index, now - BASE_LOOK - look, now)).fetchall()
            except Exception:
                log.warning("volregime: could not read %s for its baseline", index, exc_info=True)
                continue
            by_min = minute_values([float(r[0]) for r in rows], [float(r[1]) for r in rows])
            samples = []
            t = now - BASE_LOOK
            while t <= now:
                v = hourly_vol_bp(by_min, int(t // 60), look)
                if v:
                    samples.append(v)
                t += BASE_STEP
            if len(samples) >= 12:
                self.set(coin, None, statistics.median(samples))
                log.info("volregime: %s normal hourly vol %.1f bp from %d samples (%.0f h look-back)", coin, statistics.median(samples), len(samples), look / 3600.0)


regime = VolRegime()

LOOKS = {"crypto": LOOK, "commodity": 4 * 3600.0}  # the look-back each asset class is read over
DEFAULT_MAX = 1.3  # the stand-aside level until a tuned one exists (a favorite lane)
DEFAULT_MIN = 1.3  # the wild level an underdog lane needs until a tuned one exists
THRESHOLDS_FILE = "vol_thresholds.json"
TUNE_EVERY = 12 * 3600.0  # 2026-11-04 (user): every 12 hours
_CANDIDATES = (0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 1.7, 2.0)


def vol_rel_at(coin: str, index: str, at: float, look: float) -> float | None:
    """The volatility ratio a favorite saw when it was bought: the in-memory tick history over the `look` seconds before `at`, against the coin's current normal. None when the history does not reach back that far
    (an old favorite scored on a cold start) or the normal is not known: the ledger then saves no ratio for it."""
    base = regime.base(coin)
    got = ticks_mod.history.window(index, at - look - 120.0, at)
    if got is None or not base:
        return None
    vol = hourly_vol_bp(minute_values(got[0], got[1]), int(at // 60), look)
    return vol / base if vol else None


class Thresholds:
    """The tuned volatility levels per asset class, in memory, saved to data_dir/vol_thresholds.json so a restart keeps yesterday's tuning. `stand_aside` is the ratio above which a class's favorites lost (a favorite lane
    skips above it); `wild_min` is the ratio at or above which a favorite priced 0.82+ has had an underdog that paid (an underdog lane buys from it)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.stand_aside: dict[str, float] = {}
        self.wild_min: dict[str, float] = {}
        self.tuned_at = 0.0

    def get_max(self, cls: str, default: float) -> float:
        with self._lock:
            return self.stand_aside.get(cls, default)

    def get_min(self, cls: str, default: float) -> float:
        with self._lock:
            return self.wild_min.get(cls, default)

    def load(self, data_dir) -> None:
        import json

        try:
            d = json.loads((data_dir / THRESHOLDS_FILE).read_text())
        except (OSError, ValueError):
            return
        with self._lock:
            self.stand_aside, self.wild_min, self.tuned_at = dict(d.get("stand_aside", {})), dict(d.get("wild_min", {})), float(d.get("tuned_at", 0.0))

    def save(self, data_dir) -> None:
        import json
        import os

        with self._lock:
            payload = {"stand_aside": self.stand_aside, "wild_min": self.wild_min, "tuned_at": self.tuned_at}
        path = data_dir / THRESHOLDS_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1))
        os.replace(tmp, path)


thresholds = Thresholds()


def tune_thresholds(rows: list[dict], previous_max: dict[str, float] | None = None, previous_min: dict[str, float] | None = None, blend: float = 0.5,
                    min_n: int = 40, max_loss: float = 0.15, min_gain: float = 0.8) -> tuple[dict[str, float], dict[str, float]]:
    """The two levels per class from favorites that carry a volatility ratio (rows: cls, ask, pnl, flip_pnl, vol_rel).

    stand_aside: among the candidate levels, the one whose favorites ABOVE it avoided the most loss, provided they lost at least `max_loss` a $10 bet on average with `min_n` of them: skipping them is what the record
    supports. wild_min: the same for the underdog: favorites priced 0.82+ at or above the level whose flip made at least `min_gain` a bet on average, with the most total. A class with no qualifying level keeps no tuned
    value (the lane's static setting stands). A new level is blended with the previous one (`blend` of the new), so one odd day cannot swing it."""
    out_max: dict[str, float] = {}
    out_min: dict[str, float] = {}
    for cls in {r["cls"] for r in rows}:
        mine = [r for r in rows if r["cls"] == cls and r.get("vol_rel") is not None]
        scored = []
        for t in _CANDIDATES:
            above = [r["pnl"] for r in mine if r["vol_rel"] > t]
            if len(above) >= min_n and sum(above) / len(above) <= -max_loss:
                scored.append((t, -sum(above)))
        if scored:
            top = max(loss for _, loss in scored)
            tied = [t for t, loss in scored if loss >= top - 1e-9]  # levels that skip the same favorites: the middle one, not an edge of the gap
            best = tied[len(tied) // 2]
            prev = (previous_max or {}).get(cls)
            out_max[cls] = round(best if prev is None else blend * best + (1 - blend) * prev, 3)
        scoredw = []
        for t in _CANDIDATES:
            wild = [r["flip_pnl"] for r in mine if r["vol_rel"] >= t and r["ask"] >= 0.82]
            if len(wild) >= min_n and sum(wild) / len(wild) >= min_gain:
                scoredw.append((t, sum(wild)))
        if scoredw:
            topw = max(gain for _, gain in scoredw)
            tiedw = [t for t, gain in scoredw if gain >= topw - 1e-9]
            bestw = tiedw[len(tiedw) // 2]
            prev = (previous_min or {}).get(cls)
            out_min[cls] = round(bestw if prev is None else blend * bestw + (1 - blend) * prev, 3)
    return out_max, out_min


def retune(s, now: float | None = None) -> dict:
    """Read the last 7 days of recorded favorites, refit both levels, keep them in memory and on disk, and log what changed. Meant for the watcher's background thread, every 12 hours."""
    from .store import Store

    now = time.time() if now is None else now
    store = Store(s.db_path, s.exclude_versions)
    try:
        rows = [dict(zip(("cls", "ask", "pnl", "flip_pnl", "vol_rel"), (r["cls"], r["ask"], r["pnl"], r["flip_pnl"], r["vol_rel"]), strict=True))
                for r in store.db.execute("SELECT cls, ask, pnl, flip_pnl, vol_rel FROM favorite_cases WHERE vol_rel IS NOT NULL AND close_ts > ?", (now - 7 * 86400.0,)).fetchall()]
    except Exception:
        log.warning("volregime: could not read the recorded favorites", exc_info=True)
        return {}
    with thresholds._lock:
        prev_max, prev_min = dict(thresholds.stand_aside), dict(thresholds.wild_min)
    new_max, new_min = tune_thresholds(rows, prev_max, prev_min)
    with thresholds._lock:
        thresholds.stand_aside.update(new_max)
        thresholds.wild_min.update(new_min)
        thresholds.tuned_at = now
    thresholds.save(s.data_dir)
    log.info("volregime: tuned from %d favorites: stand aside above %s (was %s); underdog from %s (was %s)", len(rows), new_max, prev_max, new_min, prev_min)
    return {"stand_aside": new_max, "wild_min": new_min, "n": len(rows)}
