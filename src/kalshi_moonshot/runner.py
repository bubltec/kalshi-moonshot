"""One scan pass over two lanes, then log every decision.

moonshot: cheap long shots, a small chance of a large payout.
steady:   likely outcomes whose ask has been rising, a small payout that repeats.

For each candidate the model gets the question, similar settled cases, and recent news
headlines. Orders are placed only when MODE=trade and the risk gate agrees.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol

from . import risk
from .config import Settings
from .kalshi import KalshiClient, KalshiError
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


def candidate_markets(s: Settings, client: KalshiClient) -> list[dict]:
    """Sports fills the default market list. Skip it by walking events instead."""
    if s.skip_categories:
        return list(client.event_markets(pages=max(s.scan_pages, 10), skip_categories=s.skip_categories))
    return list(client.open_markets(pages=s.scan_pages))


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


def scan(
    s: Settings, client: KalshiClient, est: Estimator, store: Store,
    searcher: NewsSearch | None = None,
) -> list[Outcome]:
    markets = candidate_markets(s, client)
    now = datetime.now(UTC)
    moon = long_shots(markets, s, now)[: s.shortlist]
    steady: list[LongShot] = []
    if s.steady_enabled:
        taken = {x.ticker for x in moon}
        steady = [x for x in steady_shots(markets, s, client, now) if x.ticker not in taken]
    skipped = ", ".join(sorted(s.skip_categories))
    log.info(
        "%d open markets: %d moonshot, %d steady (rising ask)%s%s",
        len(markets), len(moon), len(steady),
        f", skipping {skipped}" if skipped else "",
        ", news on" if searcher else ", news off",
    )
    return [evaluate(s, client, est, store, shot, searcher) for shot in moon + steady]


def evaluate(
    s: Settings, client: KalshiClient, est: Estimator, store: Store, shot: LongShot,
    searcher: NewsSearch | None = None,
) -> Outcome:
    context = ""
    try:
        similar = store.similar_text(shot.title, shot.rules)
        context = searcher.context(shot.title, shot.subtitle) if searcher else ""
        p, why = est.estimate(
            shot.title, shot.rules, shot.close_time, similar, context, shot.subtitle
        )
    except Exception as exc:  # model down, bad JSON, timeout: skip, never trade blind
        store.add_decision(
            ticker=shot.ticker, title=shot.title, bid=shot.bid, ask=shot.ask, model_p=None,
            edge=None, action="pass", reason=f"model error: {exc}"[:200], mode=s.mode,
            close_time=shot.close_time, lane=shot.lane, context=context[:1500],
        )
        return Outcome(
            shot.ticker, shot.title, shot.ask, None, None, "pass", f"model error: {exc}", shot.lane
        )

    e = edge(p, shot.ask)
    min_edge = s.steady_min_edge if shot.lane == "steady" else s.min_edge
    bet = e >= min_edge
    note = why
    if bet and s.mode == "trade":
        note = place(s, client, store, shot, why)
    store.add_decision(
        ticker=shot.ticker, title=shot.title, bid=shot.bid, ask=shot.ask, model_p=p, edge=e,
        action="bet" if bet else "pass", reason=note[:300], mode=s.mode,
        close_time=shot.close_time, lane=shot.lane, context=context[:1500],
    )
    return Outcome(shot.ticker, shot.title, shot.ask, p, e, "bet" if bet else "pass", note, shot.lane)


def place(s: Settings, client: KalshiClient, store: Store, shot: LongShot, why: str) -> str:
    cap = min(s.max_usd_per_trade, s.steady_usd_per_trade) if shot.lane == "steady" else s.max_usd_per_trade
    count = contracts_for_budget(cap, shot.ask)
    cost = shot.ask * count + order_fee(shot.ask, count)
    verdict = risk.check(s, store, shot.ticker, cost, max_trade=cap)
    if not verdict.ok:
        return f"blocked by risk: {verdict.reason}"
    try:
        resp = client.buy_yes_ioc(shot.ticker, count, shot.ask)
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
