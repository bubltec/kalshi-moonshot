"""One scan pass: find long shots, ask the model, log every decision, and (only when MODE=trade
and the risk gate agrees) place a capped order."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from . import risk
from .config import Settings
from .kalshi import KalshiClient, KalshiError
from .store import Store
from .strategy import LongShot, contracts_for_budget, edge, long_shots, order_fee

log = logging.getLogger("moonshot")


class Estimator(Protocol):
    def estimate(self, title: str, rules: str, close_time: str) -> tuple[float, str]: ...


@dataclass
class Outcome:
    ticker: str
    title: str
    ask: float
    model_p: float | None
    edge: float | None
    action: str
    note: str


def scan(s: Settings, client: KalshiClient, est: Estimator, store: Store) -> list[Outcome]:
    markets = list(client.open_markets(pages=s.scan_pages))
    shots = long_shots(markets, s)[: s.shortlist]
    log.info("%d open markets, %d long shots, asking the model about %d", len(markets), len(shots), len(shots))
    return [evaluate(s, client, est, store, shot) for shot in shots]


def evaluate(s: Settings, client: KalshiClient, est: Estimator, store: Store, shot: LongShot) -> Outcome:
    try:
        p, why = est.estimate(shot.title, shot.rules, shot.close_time)
    except Exception as exc:  # model down, bad JSON, timeout: skip, never trade blind
        store.add_decision(
            ticker=shot.ticker, title=shot.title, bid=shot.bid, ask=shot.ask, model_p=None,
            edge=None, action="pass", reason=f"model error: {exc}"[:200], mode=s.mode,
            close_time=shot.close_time,
        )
        return Outcome(shot.ticker, shot.title, shot.ask, None, None, "pass", f"model error: {exc}")

    e = edge(p, shot.ask)
    bet = e >= s.min_edge
    note = why
    if bet and s.mode == "trade":
        note = place(s, client, store, shot, why)
    store.add_decision(
        ticker=shot.ticker, title=shot.title, bid=shot.bid, ask=shot.ask, model_p=p, edge=e,
        action="bet" if bet else "pass", reason=note[:300], mode=s.mode, close_time=shot.close_time,
    )
    return Outcome(shot.ticker, shot.title, shot.ask, p, e, "bet" if bet else "pass", note)


def place(s: Settings, client: KalshiClient, store: Store, shot: LongShot, why: str) -> str:
    count = contracts_for_budget(s.max_usd_per_trade, shot.ask)
    cost = shot.ask * count + order_fee(shot.ask, count)
    verdict = risk.check(s, store, shot.ticker, cost)
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
