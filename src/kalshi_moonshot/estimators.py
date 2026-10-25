"""Estimators: the pluggable part of a lane. Each turns a shortlisted market into a probability.

llm     asks the local model, giving it similar settled cases, their base rate, and fresh headlines
ladder  prices a commodity threshold from the strike ladder and a live feed (commodities.py)
llm_shape  asks the local model about a 15-minute window, showing it the window's price shape so far and the closest past windows (shape.py)
market  has no opinion: it returns the market's own price, so a lane using it records what buying a price band earns

A lane names its estimator in its spec; runner.py looks it up here and knows nothing else about it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from . import shape
from .commodities import CommodityError, CommodityPricer
from .config import Settings
from .lanes import LADDER, LLM, MARKET, SHAPE, LaneSpec
from .search import NewsSearch
from .store import Store
from .strategy import LongShot, edge, side_prob

log = logging.getLogger("moonshot")


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


class EstimateShare:
    """One estimate per (estimator, market), reused by every lane that asks within `ttl` seconds.

    An estimate is the chance the market resolves YES. It does not depend on which lane asks, so a hundred lanes watching one market need one model call or one ladder
    pricing, not a hundred. The market-price estimator is never shared (its answer is the lane's own asked price), failures are never kept, and the order a live lane
    places still reads the real market afterwards, so a shared estimate never stands in for a price.
    """

    UNSHARED = frozenset({"market"})

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._got: dict[tuple[str, str], tuple[float, Estimate]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, name: str, shot: LongShot, ttl: float, compute: Callable[[], Estimate]) -> Estimate:
        if ttl <= 0 or name in self.UNSHARED:
            return compute()
        key = (name, shot.ticker)
        with self._lock:
            hit = self._got.get(key)
            if hit and self._clock() - hit[0] <= ttl:
                self.hits += 1
                return hit[1]
            self.misses += 1
        got = compute()
        with self._lock:
            self._got[key] = (self._clock(), got)
            if len(self._got) > 2000:  # drop what has aged out; a few thousand markets at most are ever live
                cutoff = self._clock() - ttl
                self._got = {k: v for k, v in self._got.items() if v[0] >= cutoff}
        return got


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


class LLMShapeEstimator:
    """The local model prices a 15-minute window from its shape so far and the closest past windows (shape.py).

    A model call takes seconds and the worker runs one job at a time, so the call never runs inside a lane pass: `screen` builds the prompt,
    sends it to a single background thread, and keeps only the markets whose answer has come back (a minute or two later). `estimate` reads that answer.
    """

    TTL = 150.0  # an answer is used for this long, then the market is asked again from its newer shape
    MAX_PENDING = 3
    MIN_POOL = 30  # fewer settled windows than this and there is nothing to show the model

    def __init__(self, model: Any, store: Store, executor: Any = None):
        self.model, self.store = model, store
        self._exec = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="shape")
        self._lock = threading.Lock()
        self._ready: dict[str, tuple[float, float, str, str, int]] = {}  # ticker -> (time, p_yes, why, prompt, minutes of shape)
        self._pending: set[str] = set()
        self._failed: dict[str, float] = {}
        self._pools: dict[str, tuple[float, list[shape.Window]]] = {}
        self._tape: Any = None

    # -- data (runs on the caller's thread: quick database reads) --
    def _pool(self, series: str, now_ts: float) -> list[shape.Window]:
        hit = self._pools.get(series)
        if hit and now_ts - hit[0] < 120.0:
            return hit[1]
        rows = self.store.db.execute(
            "SELECT w.ticker, w.close_ts, w.result, c.end_ts, c.bid_c, c.ask_c FROM windows w JOIN candles c ON c.ticker = w.ticker "
            "WHERE w.series = ? AND w.result IN ('yes','no') AND w.close_ts < ? ORDER BY w.ticker, c.end_ts", (series, now_ts),
        ).fetchall()
        by: dict[str, list] = {}
        meta: dict[str, tuple[float, str]] = {}
        for r in rows:
            by.setdefault(r[0], []).append((r[3], r[4], r[5]))
            meta[r[0]] = (float(r[1]), r[2])
        pool = [shape.Window(tk, meta[tk][0], meta[tk][1], shape.path_from_candles(c, meta[tk][0])) for tk, c in by.items()]
        self._pools[series] = (now_ts, pool)
        return pool

    def _quotes(self, s: Settings, ticker: str, open_ts: float, now_ts: float) -> list:
        if self._tape is None:
            from .tape import Tape

            self._tape = Tape(s.tape_path)
        rows = self._tape.db.execute(
            "SELECT ts, bid, ask FROM quotes WHERE ticker = ? AND ts BETWEEN ? AND ? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts",
            (ticker, open_ts - 5.0, now_ts),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def _prompt(self, shot: LongShot, s: Settings, now_ts: float) -> tuple[str, int] | None:
        try:
            close_ts = datetime.fromisoformat(shot.close_time).timestamp()
        except (TypeError, ValueError):
            return None
        open_ts = close_ts - shape.WINDOW_SECONDS
        minute = int((now_ts - open_ts) // 60) + 1
        done = minute - 1
        if not 2 <= done <= shape.WINDOW_MINUTES - 1:
            return None
        current = shape.path_from_quotes(self._quotes(s, shot.ticker, open_ts, now_ts), open_ts, done)
        pool = self._pool(shot.series or shot.ticker.split("-")[0], now_ts)
        if len(pool) < self.MIN_POOL or sum(v is not None for v in current) < max(2, done - 2):
            return None
        neighbors = shape.nearest(current, pool, done)
        bid, ask = (shot.bid, shot.ask) if shot.side == "yes" else (1.0 - shot.ask, 1.0 - shot.bid)
        return shape.build_prompt(shot.title, minute, bid, ask, current, neighbors, pool), done

    def hold_chance(self, s: Settings, shot: LongShot, timeout: float = 8.0) -> float | None:
        """The model's chance that the side in `shot` pays, asked now and waited for up to `timeout` seconds; None when it cannot say in time (a stop then sells)."""
        try:
            built = self._prompt(shot, s, time.time())
        except Exception:
            return None
        if built is None:
            return None
        fut: Future = self._exec.submit(self.model.ask, built[0], 80, shot.ticker.split("-")[0])
        try:
            p, _ = fut.result(timeout=timeout)
        except Exception:
            fut.cancel()
            return None
        return side_prob(p, shot.side)

    # -- the estimator interface --
    def screen(self, shots, markets, spec, s, now):
        now_ts = now.timestamp()
        keep, started = [], set()
        for shot in shots:
            with self._lock:
                got = self._ready.get(shot.ticker)
            if got is not None and now_ts - got[0] <= self.TTL:
                keep.append(shot)
            elif shot.ticker not in started:
                started.add(shot.ticker)
                self._start(shot, s, now_ts)
        return keep

    def _start(self, shot: LongShot, s: Settings, now_ts: float) -> None:
        with self._lock:
            if shot.ticker in self._pending or len(self._pending) >= self.MAX_PENDING or now_ts - self._failed.get(shot.ticker, 0.0) < 60.0:
                return
        try:
            built = self._prompt(shot, s, now_ts)
        except Exception as exc:
            log.warning("shape: could not build %s: %s", shot.ticker, exc)
            return
        if built is None:
            return
        prompt, done = built
        with self._lock:
            self._pending.add(shot.ticker)
        ticker = shot.ticker
        fut: Future = self._exec.submit(self.model.ask, prompt, 80, ticker.split("-")[0])

        def finished(f: Future) -> None:
            self._finish(ticker, f, prompt, done, now_ts)

        fut.add_done_callback(finished)

    def _finish(self, ticker: str, fut: Future, prompt: str, done: int, asked_ts: float) -> None:
        try:
            p, why = fut.result()
        except Exception as exc:
            log.warning("shape: model failed for %s: %s", ticker, exc)
            with self._lock:
                self._pending.discard(ticker)
                self._failed[ticker] = asked_ts
            return
        with self._lock:
            self._pending.discard(ticker)
            self._ready[ticker] = (asked_ts, p, why, prompt, done)  # aged from when the shape was read

    def estimate(self, client: Any, s: Settings, shot: LongShot) -> Estimate:
        with self._lock:
            got = self._ready.get(shot.ticker)
        if got is None or time.time() - got[0] > self.TTL + 90.0:
            raise EstimateFailed(RuntimeError("shape estimate is not ready"))
        _, p, why, prompt, done = got
        return Estimate(p, f"model on the {done}-minute shape: {why}", prompt)


class MarketEstimator:
    """No model: the chance is the price asked, so edge is zero and an `always` lane decides on its filters alone."""

    def screen(self, shots, markets, spec, s, now):
        return shots

    def estimate(self, client: Any, s: Settings, shot: LongShot) -> Estimate:
        p_yes = shot.ask if shot.side == "yes" else 1.0 - shot.ask
        return Estimate(p_yes, f"no model: priced at the {shot.ask:.2f} asked", "")


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
    out: dict[str, Estimator] = {LLM: LLMEstimator(model, store, searcher), MARKET: MarketEstimator()}
    if hasattr(model, "ask"):
        out[SHAPE] = _shape_estimator(model, store)  # one per model and store: it holds the answers that come back between passes
    if quant is not None:
        out[LADDER] = LadderEstimator(quant)
    return out


_SHAPE: dict[tuple[int, int], LLMShapeEstimator] = {}


def _shape_estimator(model: Any, store: Store) -> LLMShapeEstimator:
    key = (id(model), id(store))
    if key not in _SHAPE:
        _SHAPE[key] = LLMShapeEstimator(model, store)
    return _SHAPE[key]
