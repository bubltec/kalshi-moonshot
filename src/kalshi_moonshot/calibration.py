"""Let our own record set how much to believe an estimate, instead of a fixed rule.

Two measurements, both from settled markets (each lane's first rating of each market):

  skill   1 - (model Brier / market Brier) for the lane. Zero or less means the estimator has not
          beaten simply believing the ask, so its weight goes to zero as evidence builds.
  band    how often markets in the same lane and price band (and category, when there is enough
          data) actually resolved yes. Starts at the ask itself and moves only as outcomes arrive
          (a Beta prior centered on the ask, worth PRIOR_STRENGTH observations).

The probability used for the bet is   w * estimate + (1 - w) * band,
where w starts at the lane's `trust` and slides to the measured skill as settled markets accumulate.
With no record at all, the estimate passes through unchanged.

Where the record is read (2026-11-16, user: "Nothing in per tick computations should do a db read. Ever. We can do structured background jobs to build a template for a market but never per tick"): the running bot calls start_background once. From then on a background thread rebuilds each lane's summary from the settled decisions every REBUILD_EVERY seconds, and adjust() answers from that summary in memory: it never touches the database. Before that change every evaluation thread reloaded every settled decision (132,508 rows on 2026-11-16) each time any bet settled, and re-scored the lane's whole record on every evaluation. A process that never calls start_background (a command, a test) reads the record on demand as before.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from .report import _yes, first_per_market

if TYPE_CHECKING:
    from .store import Store

PRIOR_STRENGTH = 20.0  # observations the prior (ask, trust) is worth
BAND_WIDTH = 0.05  # price bucket for the band hit rate
REBUILD_EVERY = 900.0  # seconds between background rebuilds of the record the bot serves from memory. A lane's record moves slowly; at 120 s the first version (which also read every column) coincided with 12 s stalls of live commodity evaluations on 2026-11-16 22:52
MIN_BAND_N = 8  # settled markets in a category band before it overrides the lane-wide band


@dataclass(frozen=True)
class Calibration:
    p: float  # probability to bet on
    weight: float  # weight on the estimate
    band_p: float  # what our record says for this price band
    band_n: int
    skill: float | None
    n: int  # settled markets in the lane

    def note(self, p_model: float) -> str:
        if self.n == 0:
            return "no settled record for this lane yet; estimate used as is"
        skill = "n/a" if self.skill is None else f"{self.skill:+.2f}"
        return (
            f"record-adjusted {p_model:.2f} -> {self.p:.2f} (weight on estimate {self.weight:.2f}, "
            f"skill {skill} over {self.n} settled; band hit rate {self.band_p:.2f} from {self.band_n})"
        )


def _bucket(ask: float) -> int:
    return int(ask / BAND_WIDTH + 1e-9)


log = logging.getLogger(__name__)


@dataclass
class Summary:
    """One lane's settled record, reduced to what adjust() needs: O(1) to read."""

    n: int = 0
    mkt_sq: float = 0.0  # sum of squared errors of the ask
    mod_sq: float = 0.0  # sum of squared errors of the model's chance
    bands: dict[int, list[int]] = field(default_factory=dict)  # price bucket -> [hits, markets]
    cats: dict[tuple[int, str], list[int]] = field(default_factory=dict)  # (price bucket, category) -> [hits, markets]


def summarize(settled: list[Any], pause: float = 0.0) -> dict[str, Summary]:
    """Each lane's summary from the settled decisions. One rating per market per lane: a lane's record is its own first look at each market, whatever other lanes rated it before."""
    by_lane: dict[str, list] = {}
    for i, r in enumerate(settled):
        by_lane.setdefault(r["lane"] or "moonshot", []).append(r)
        if pause and i % 2000 == 1999:
            time.sleep(pause)  # the background build steps aside for the threads that trade
    out: dict[str, Summary] = {}
    for lane, rows in by_lane.items():
        sm = Summary()
        for r in first_per_market(rows):
            y = _yes(r)
            sm.n += 1
            sm.mkt_sq += (r["ask"] - y) ** 2
            sm.mod_sq += (r["model_p"] - y) ** 2
            b = _bucket(r["ask"])
            cell = sm.bands.setdefault(b, [0, 0])
            cell[0] += y
            cell[1] += 1
            cell = sm.cats.setdefault((b, r["category"] or ""), [0, 0])
            cell[0] += y
            cell[1] += 1
        out[lane] = sm
    return out


_SERVED: dict[str, tuple[float, dict[str, Summary]]] = {}  # database path -> (built at, summaries): what the running bot answers from
_SERVE_LOCK = threading.Lock()


def rebuild(store: Store) -> dict[str, Summary]:
    """Read the settled decisions and replace the served record for this store's database. The one place the running bot reads them; called at start and by the background thread, never from an evaluation."""
    got = summarize(store.settled_for_record(), pause=0.002)
    with _SERVE_LOCK:
        _SERVED[str(store.path)] = (time.time(), got)
    return got


def start_background(get_store: Callable[[], Store], every: float = REBUILD_EVERY) -> None:
    """Build the record once now (the bot is starting: nothing is being evaluated yet), then keep it fresh from a background thread with its own store."""
    try:
        rebuild(get_store())
    except Exception:
        log.exception("calibration: the first build failed; evaluations read the record on demand until a rebuild succeeds")

    def loop() -> None:
        store = None
        while True:
            time.sleep(every)
            try:
                store = store or get_store()
                rebuild(store)
            except Exception:
                store = None
                log.exception("calibration: rebuild failed; the last record stays in use")

    threading.Thread(target=loop, name="calibration", daemon=True).start()


def stop_serving() -> None:
    """Forget the served record (tests)."""
    with _SERVE_LOCK:
        _SERVED.clear()


class Calibrator:
    def __init__(self, store: Store, prior_strength: float = PRIOR_STRENGTH):
        self.store = store
        self.k = prior_strength
        self._cache: tuple[int, dict[str, Summary]] | None = None

    def _lanes(self) -> dict[str, Summary]:
        served = _SERVED.get(str(self.store.path))
        if served is not None:
            return served[1]  # the running bot: memory only
        gen = self.store.generation
        if self._cache and self._cache[0] == gen:
            return self._cache[1]
        lanes = summarize(self.store.settled_for_record())
        self._cache = (gen, lanes)
        return lanes

    def adjust(self, lane: str, category: str, ask: float, p_model: float, trust: float = 1.0) -> Calibration:
        sm = self._lanes().get(lane)
        n = sm.n if sm else 0
        if n == 0:
            return Calibration(p_model, 1.0, ask, 0, None, 0)
        mkt, mod = sm.mkt_sq / n, sm.mod_sq / n
        skill = 1.0 - mod / mkt if mkt > 0 else None
        measured = 0.0 if skill is None else min(max(skill, 0.0), 1.0)
        w = (n * measured + self.k * trust) / (n + self.k)

        hits, band_n = sm.bands.get(_bucket(ask), (0, 0))
        in_cat = sm.cats.get((_bucket(ask), category), (0, 0))
        if category and in_cat[1] >= MIN_BAND_N:
            hits, band_n = in_cat
        band_p = (hits + self.k * ask) / (band_n + self.k)
        p = min(max(w * p_model + (1.0 - w) * band_p, 0.001), 0.999)
        return Calibration(p, w, band_p, band_n, skill, n)
