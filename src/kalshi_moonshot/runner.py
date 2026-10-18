"""One scan pass over two lanes, then log every decision.

moonshot: cheap long shots, a small chance of a large payout.
steady:   likely outcomes whose ask has been rising, a small payout that repeats.

For each candidate the model gets the question, similar settled cases, and recent news
headlines. Orders are placed only when MODE=trade and the risk gate agrees.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from . import risk
from .config import Settings
from .kalshi import MARKET_PAGES, KalshiClient, KalshiError, KalshiRateLimited
from .search import NewsSearch
from .store import Store
from .strategy import (
    LongShot,
    ask_trend,
    contracts_for_budget,
    edge,
    long_shots,
    order_fee,
    steady_candidates,
    trend_ok,
)

log = logging.getLogger("moonshot")


class Estimator(Protocol):
    def estimate(
        self, title: str, rules: str, close_time: str, similar: str = "",
        context: str = "", subtitle: str = "",
    ) -> tuple[float, str]: ...


@dataclass
class Outcome:
    ticker: str
    title: str
    ask: float
    model_p: float | None
    edge: float | None
    action: str
    note: str
    lane: str = "moonshot"


def candidate_markets(s: Settings, client: KalshiClient, partial_ok: bool = True) -> list[dict]:
    """Open markets closing inside the window either lane is looking at, with category attached.

    Fetched by close time. The plain market list and the event list are not ordered by close
    time, so reading the first few pages can return zero markets inside a short window.

    If a page fails after every retry: partial_ok keeps what was read (with a warning),
    otherwise the error is raised so a queued job can retry the whole fetch later.
    """
    lo, hi = s.min_hours_to_close, s.max_hours_to_close
    if s.steady_enabled:
        lo = min(lo, s.steady_min_hours_to_close)
        hi = max(hi, s.steady_max_hours_to_close)
    now = time.time()
    info = client.event_info()
    skip = {c.casefold() for c in s.skip_categories}
    out: list[dict] = []
    fetched = 0
    try:
        for m in client.markets_closing(now + lo * 3600, now + hi * 3600):
            fetched += 1
            category, series = info.get(m.get("event_ticker", ""), ("", ""))
            if category and category.casefold() in skip:
                continue
            m["category"] = category
            if series:
                m.setdefault("series_ticker", series)
            out.append(m)
    except KalshiError as exc:
        if not partial_ok:
            raise
        log.warning("market fetch stopped after %d markets (%s); using what was read", fetched, exc)
    if fetched >= MARKET_PAGES * 200:
        log.warning(
            "fetched the %d-market limit for a %.0f-%.0fh window; some markets were not read. "
            "Narrow the hour limits.", fetched, lo, hi,
        )
    return out


def steady_shots(
    markets: list[dict], s: Settings, client: KalshiClient, now: datetime | None = None
) -> list[LongShot]:
    """Steady-lane candidates whose ask has been rising and is not rolling over."""
    pool = steady_candidates(markets, s, now)[: s.steady_shortlist * 3]
    out: list[LongShot] = []
    for shot in pool:
        try:
            candles = client.candlesticks(shot.series, shot.ticker, s.steady_lookback_hours)
        except KalshiError:
            continue
        t = ask_trend(candles)
        if t is None or not trend_ok(t, s):
            continue
        out.append(replace(shot, trend=t.change))
        if len(out) >= s.steady_shortlist:
            break
    return out


def shortlist(
    s: Settings, client: KalshiClient, markets: list[dict], now: datetime | None = None
) -> tuple[list[LongShot], list[LongShot]]:
    """(moonshot, steady) candidates for the model, from an already-fetched market list."""
    now = now or datetime.now(UTC)
    moon = long_shots(markets, s, now)[: s.shortlist]
    steady: list[LongShot] = []
    if s.steady_enabled:
        taken = {x.ticker for x in moon}
        steady = [x for x in steady_shots(markets, s, client, now) if x.ticker not in taken]
    return moon, steady


def describe_scan(s: Settings, total: int, moon: int, steady: int, searcher: object | None) -> str:
    skipped = ", ".join(sorted(s.skip_categories))
    return (
        f"{total} open markets: {moon} moonshot, {steady} steady (rising ask)"
        f"{f', skipping {skipped}' if skipped else ''}"
        f"{', news on' if searcher else ', news off'}"
    )


def format_outcome(r: Outcome) -> str:
    p = f"{r.model_p:.3f}" if r.model_p is not None else "  -  "
    e = f"{r.edge:+.3f}" if r.edge is not None else "  -  "
    return (
        f"{r.action:4} {r.lane[:4]:4} ask={r.ask:.2f} model={p} edge={e}  {r.ticker}\n"
        f"     {r.title[:90]} | {r.note[:110]}"
    )


def scan(
    s: Settings, client: KalshiClient, est: Estimator, store: Store,
    searcher: NewsSearch | None = None,
) -> list[Outcome]:
    """One synchronous pass (the `scan` command). The `run` loop uses the job queue instead."""
    markets = candidate_markets(s, client)
    moon, steady = shortlist(s, client, markets)
    log.info(describe_scan(s, len(markets), len(moon), len(steady), searcher))
    return [evaluate(s, client, est, store, shot, searcher) for shot in moon + steady]


def evaluate(
    s: Settings, client: KalshiClient, est: Estimator, store: Store, shot: LongShot,
    searcher: NewsSearch | None = None,
    *,
    raise_model_errors: bool = False,
    order_key: str | None = None,
    before_order: Callable[[], None] | None = None,
) -> Outcome:
    """Rate one market, log the decision, and place an order if allowed.

    raise_model_errors: let a model failure propagate (so a queued job can retry it) instead
    of logging a "model error" pass. order_key makes the order id stable across retries.
    before_order runs just before an order is sent, so the caller can mark that moment.
    """
    context = ""
    try:
        similar = store.similar_text(shot.title, shot.rules)
        context = searcher.context(shot.title, shot.subtitle) if searcher else ""
        p, why = est.estimate(
            shot.title, shot.rules, shot.close_time, similar, context, shot.subtitle
        )
    except Exception as exc:  # model down, bad JSON, timeout: skip, never trade blind
        if raise_model_errors:
            raise
        store.add_decision(
            ticker=shot.ticker, title=shot.title, bid=shot.bid, ask=shot.ask, model_p=None,
            edge=None, action="pass", reason=f"model error: {exc}"[:200], mode=s.mode,
            close_time=shot.close_time, lane=shot.lane, context=context[:1500],
            category=shot.category,
        )
        return Outcome(
            shot.ticker, shot.title, shot.ask, None, None, "pass", f"model error: {exc}", shot.lane
        )

    e = edge(p, shot.ask)
    min_edge = s.steady_min_edge if shot.lane == "steady" else s.min_edge
    bet = e >= min_edge
    note = why
    if bet and s.mode == "trade":
        note = place(s, client, store, shot, why, order_key=order_key, before_order=before_order)
    store.add_decision(
        ticker=shot.ticker, title=shot.title, bid=shot.bid, ask=shot.ask, model_p=p, edge=e,
        action="bet" if bet else "pass", reason=note[:300], mode=s.mode,
        close_time=shot.close_time, lane=shot.lane, context=context[:1500],
        category=shot.category,
    )
    return Outcome(shot.ticker, shot.title, shot.ask, p, e, "bet" if bet else "pass", note, shot.lane)


def place(
    s: Settings, client: KalshiClient, store: Store, shot: LongShot, why: str,
    order_key: str | None = None, before_order: Callable[[], None] | None = None,
) -> str:
    cap = min(s.max_usd_per_trade, s.steady_usd_per_trade) if shot.lane == "steady" else s.max_usd_per_trade
    count = contracts_for_budget(cap, shot.ask)
    cost = shot.ask * count + order_fee(shot.ask, count)
    verdict = risk.check(s, store, shot.ticker, cost, max_trade=cap)
    if not verdict.ok:
        return f"blocked by risk: {verdict.reason}"
    if before_order:
        before_order()
    try:
        resp = client.buy_yes_ioc(shot.ticker, count, shot.ask, client_order_id=order_key)
    except KalshiRateLimited:
        raise  # refused outright, so nothing was bought; the queue retries it later
    except KalshiError as exc:
        return f"order failed: {exc}"
    filled = float(resp.get("fill_count") or 0.0)
    avg = float(resp.get("average_fill_price") or shot.ask)
    fee = float(resp.get("average_fee_paid") or 0.0) * filled
    store.add_order(
        ticker=shot.ticker, env=s.env, count=count, price=shot.ask, fill_count=filled,
        cost_usd=avg * filled + fee, fee_usd=fee, order_id=resp.get("order_id", ""),
    )
    return f"bought {filled:g}/{count} @ {avg:.4f} ({s.env}). {why}"


def resolve(client: KalshiClient, store: Store) -> int:
    """Fill in outcomes for markets that have settled. Returns how many markets resolved."""
    n = 0
    for ticker in store.unresolved_tickers():
        try:
            m = client.market(ticker)
        except KalshiError:
            continue
        if m.get("result") in {"yes", "no"}:
            store.set_outcome(ticker, m["result"])
            n += 1
    return n
