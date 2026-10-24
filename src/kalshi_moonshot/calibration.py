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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .report import _yes, brier, first_per_market

if TYPE_CHECKING:
    from .store import Store

PRIOR_STRENGTH = 20.0  # observations the prior (ask, trust) is worth
BAND_WIDTH = 0.05  # price bucket for the band hit rate
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


class Calibrator:
    def __init__(self, store: Store, prior_strength: float = PRIOR_STRENGTH):
        self.store = store
        self.k = prior_strength
        self._cache: tuple[int, dict[str, list]] | None = None

    def _lanes(self) -> dict[str, list]:
        gen = self.store.generation
        if self._cache and self._cache[0] == gen:
            return self._cache[1]
        by_lane: dict[str, list] = {}
        for r in self.store.settled_decisions():
            by_lane.setdefault(r["lane"] or "moonshot", []).append(r)
        # One rating per market *per lane*: a lane's record is its own first look at each market, whatever other lanes rated it before.
        # (Counting a market only for the first lane to rate it left newer lanes, which share markets with older ones, with no record at all.)
        lanes = {lane: first_per_market(rows) for lane, rows in by_lane.items()}
        self._cache = (gen, lanes)
        return lanes

    def adjust(self, lane: str, category: str, ask: float, p_model: float, trust: float = 1.0) -> Calibration:
        rows = self._lanes().get(lane, [])
        n = len(rows)
        if n == 0:
            return Calibration(p_model, 1.0, ask, 0, None, 0)
        mkt = brier([(r["ask"], _yes(r)) for r in rows])
        mod = brier([(r["model_p"], _yes(r)) for r in rows])
        skill = 1.0 - mod / mkt if mkt > 0 else None
        measured = 0.0 if skill is None else min(max(skill, 0.0), 1.0)
        w = (n * measured + self.k * trust) / (n + self.k)

        band = [r for r in rows if _bucket(r["ask"]) == _bucket(ask)]
        in_cat = [r for r in band if (r["category"] or "") == category]
        if category and len(in_cat) >= MIN_BAND_N:
            band = in_cat
        hits = sum(_yes(r) for r in band)
        band_p = (hits + self.k * ask) / (len(band) + self.k)
        p = min(max(w * p_model + (1.0 - w) * band_p, 0.001), 0.999)
        return Calibration(p, w, band_p, len(band), skill, n)
