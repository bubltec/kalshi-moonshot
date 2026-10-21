"""Estimators: the pluggable part of a lane. Each turns a shortlisted market into a probability.

llm     asks the local model, giving it similar settled cases, their base rate, and fresh headlines
ladder  prices a commodity threshold from the strike ladder and a live feed (commodities.py)

A lane names its estimator in its spec; runner.py looks it up here and knows nothing else about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .commodities import CommodityError, CommodityPricer
from .config import Settings
from .lanes import LADDER, LLM, LaneSpec
from .search import NewsSearch
from .store import Store
from .strategy import LongShot, edge, side_prob


@dataclass(frozen=True)
class Estimate:
    p: float
    why: str
    context: str = ""  # what the estimator was shown or computed; stored with the decision


class EstimateFailed(RuntimeError):
    """An estimator could not answer. `context` keeps whatever it had gathered by then."""

    def __init__(self, cause: Exception, context: str = ""):
        super().__init__(str(cause))
        self.__cause__ = cause
        self.context = context


class Estimator(Protocol):
    def estimate(self, client: Any, s: Settings, shot: LongShot) -> Estimate: ...

    def screen(
        self, shots: list[LongShot], markets: list[dict], spec: LaneSpec, s: Settings, now: datetime
    ) -> list[LongShot]: ...


class LLMEstimator:
    def __init__(self, model: Any, store: Store, searcher: NewsSearch | None = None):
        self.model, self.store, self.searcher = model, store, searcher

    def screen(self, shots, markets, spec, s, now):
        return shots  # every candidate costs a model call, so the lane's own filters decide

    def estimate(self, client: Any, s: Settings, shot: LongShot) -> Estimate:
        context = ""
        try:
            similar = self.store.rag.block(
                shot.title, shot.rules, ticker=shot.ticker, series=shot.series,
                category=shot.category, lane=shot.lane,
            )
            context = self.searcher.context(shot.title, shot.subtitle) if self.searcher else ""
            p, why = self.model.estimate(
                shot.title, shot.rules, shot.close_time, similar, context, shot.subtitle
            )
        except Exception as exc:
            raise EstimateFailed(exc, context) from exc
        return Estimate(p, why, context)


class LadderEstimator:
    def __init__(self, pricer: CommodityPricer):
        self.pricer = pricer
        self.dropped: list[dict[str, Any]] = []  # the last screen's rejects, with why (read by the review pass)

    def estimate(self, client: Any, s: Settings, shot: LongShot) -> Estimate:
        try:
            p, why = self.pricer.estimate(client, shot, s)
        except Exception as exc:
            raise EstimateFailed(exc) from exc
        return Estimate(p, why, why)

    def screen(self, shots, markets, spec, s, now):
        """Price each event's ladder once; keep markets whose edge clears the lane's bar."""
        from .lanes import chance_ok, wants_bet

        by_event: dict[str, list[dict]] = {}
        for m in markets:
            by_event.setdefault(m.get("event_ticker", ""), []).append(m)
        # Richest ladders first: a fitted ladder teaches the pricer the feed-to-ladder basis, which
        # the thin near-expiry ladders of the same instrument then reuse.
        wanted = {shot.event_ticker for shot in shots}
        priced: dict[str, dict[str, tuple[float, str]]] = {}
        for event in sorted(wanted, key=lambda ev: -len(by_event.get(ev, []))):
            try:
                priced[event] = self.pricer.price_markets(by_event.get(event, []), s, now)
            except CommodityError:
                priced[event] = {}
        keep: list[tuple[float, LongShot]] = []
        self.dropped = []
        for shot in shots:
            hit = priced[shot.event_ticker].get(shot.ticker)
            if hit is None:
                self.dropped.append({"ticker": shot.ticker, "side": shot.side, "ask": shot.ask,
                                     "reason": "could not price this ladder (no feed or too few strikes)"})
                continue
            p_side = side_prob(hit[0], shot.side)
            e = edge(p_side, shot.ask)
            if wants_bet(spec, p_side, shot.ask, e) and chance_ok(spec, p_side):
                keep.append((e, shot))
            else:
                bar = f"needs {spec.min_edge:+.1%}" if spec.min_edge is not None else "outside the lane's chance band"
                self.dropped.append({"ticker": shot.ticker, "side": shot.side, "ask": shot.ask, "p": p_side, "edge": e,
                                     "reason": f"our chance {p_side:.0%} vs ask {shot.ask:.2f}: edge {e:+.1%}, {bar}. {hit[1][:120]}"})
        keep.sort(key=lambda x: x[0], reverse=True)
        return [shot for _, shot in keep]


def build_estimators(
    model: Any, store: Store, searcher: NewsSearch | None, quant: CommodityPricer | None
) -> dict[str, Estimator]:
    out: dict[str, Estimator] = {LLM: LLMEstimator(model, store, searcher)}
    if quant is not None:
        out[LADDER] = LadderEstimator(quant)
    return out
