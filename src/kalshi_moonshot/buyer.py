"""The buyer: the one place a watcher's buy signal becomes a bet.

A watcher only says what it saw (a BuySignal: this side, this price, this reason). The policy lives here and comes from the lane:
what to stake, how many bets the lane may hold, whether the lane buys for real or on paper, whether a series must have won first,
and the allowance over the ask. hunt_plan and flip_plan read that policy into a BuyPlan; buy() carries a plan out.

A live order that buys nothing is still logged, as an observe bet with why it missed (context["miss"]), so paper bets and missed orders can be told apart.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import runner
from .config import Settings
from .kalshi import KalshiRateLimited
from .lanes import LaneSpec
from .store import Store
from .strategy import LongShot

log = logging.getLogger("moonshot")

ALLOWANCE = 0.02  # a live buy's limit is this much over the ask, so a quote that ticked up since it was read still fills; the stake is sized with it
FLIP_LANE = "burst_flip"  # the lane a bet on the other side of a held favorite is logged under


def whole_contracts(stake: float, ask: float) -> float:
    """The whole contracts a live order for this stake asks for: the stake over the ask plus the allowance, at least one. A paper bet is sized the same way, so the two can be compared."""
    return float(max(1, int(stake / (ask + ALLOWANCE))))


def category(ticker: str) -> str:
    return "Crypto" if ticker.split("-")[0][2:].startswith(("BTC", "ETH", "SOL", "XRP", "DOGE")) else "Other"


@dataclass
class BuyPlan:
    """What one buy needs, with the lane's policy already applied."""
    ticker: str
    side: str
    ask: float
    reason: str
    ctx: dict[str, Any]
    venue: Any  # the client that holds the market
    decision_lane: str  # the lane the bet is logged under
    live_lane: str  # the lane whose live setting decides real or paper
    stake: float  # dollars a live order is sized to
    paper_contracts: float  # what a paper bet logs
    paper_stake: float
    close_ts: float
    shot_close: str  # close time handed to the order
    log_close: str  # close time written on the decision
    label: str  # the log prefix
    cap_lane: str | None = None  # the lane whose open bets cap this buy (None = no cap)
    max_open: int = 0  # 0 = no limit
    require_prior_win: bool = False
    series: str = field(init=False)

    def __post_init__(self) -> None:
        self.series = self.ticker.split("-")[0]


def hunt_plan(spec: LaneSpec, ticker: str, side: str, ask: float, close_ts: float, reason: str, ctx: dict[str, Any], venue: Any) -> BuyPlan:
    """The cheap side of a market no one holds, bought under the hunting lane's own rules."""
    ask = round(ask, 4)
    stake = spec.fixed_stake or 2.0
    close = datetime.fromtimestamp(close_ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if close_ts else ""
    count = whole_contracts(stake, ask)
    return BuyPlan(
        ticker, side, ask, reason, ctx, venue, decision_lane=spec.name, live_lane=spec.name, stake=stake,
        paper_contracts=count, paper_stake=round(count * ask, 4), close_ts=close_ts, shot_close=close, log_close=close,
        label="burst hunt", cap_lane=spec.name, max_open=spec.max_open, require_prior_win=spec.require_prior_win,
    )


def flip_plan(held: Any, side: str, reason: str, ctx: dict[str, Any], venue: Any) -> BuyPlan:
    """The other side of a held favorite whose burst is selling it: a fixed small stake (the held lane's burst_buy), live when the holding lane is."""
    ask = round(held.cur_other_ask, 4)
    return BuyPlan(
        held.ticker, side, ask, reason, ctx, venue, decision_lane=FLIP_LANE, live_lane=held.lane, stake=held.burst_buy,
        paper_contracts=1.0, paper_stake=ask, close_ts=held.close_ts, shot_close=datetime.fromtimestamp(held.close_ts, UTC).isoformat(), log_close="",
        label="burst buy",
    )


def _has_won(store: Store, lane: str, series: str) -> bool:
    return store.db.execute(
        "SELECT 1 FROM decisions d JOIN windows w ON w.ticker = d.ticker WHERE d.lane=? AND d.action='bet' AND d.ticker LIKE ? AND w.result IN ('yes','no') AND w.result = d.side LIMIT 1",
        (lane, series + "-%"),
    ).fetchone() is not None


def buy(plan: BuyPlan, s: Settings, store: Store) -> None:
    """Carry out one buy: skip it if the lane is full or already holds the market, place it for real if the lane is live (a paper bet otherwise), and log the decision."""
    tk, ask, reason = plan.ticker, plan.ask, plan.reason
    if plan.cap_lane is not None:
        open_same = [r for r in store.open_bets() if (r["lane"] or "") == plan.cap_lane]
        if plan.max_open and len(open_same) >= plan.max_open:
            log.info("%s skipped, %s %s: %s already holds %d of its %d open bets", plan.label, tk, plan.side, plan.cap_lane, len(open_same), plan.max_open)
            return
        if any(r["ticker"] == tk for r in open_same):
            log.info("%s skipped, %s %s: %s already holds this market", plan.label, tk, plan.side, plan.cap_lane)
            return
    live = s.is_live(plan.live_lane, plan.series) and getattr(plan.venue, "authed", False)
    if live and plan.require_prior_win and not _has_won(store, plan.decision_lane, plan.series):
        live = False  # this series has not won for the lane yet: a paper bet only, so it can earn its way in
        reason = f"{reason} (paper: {plan.series} has no win yet)"
    filled, cost, note = 0.0, 0.0, reason
    if live:
        shot = LongShot(tk, "", "", max(0.0, ask - 0.01), min(0.99, ask + ALLOWANCE), 0.0, plan.shot_close, max(0.0, (plan.close_ts - time.time()) / 3600.0),
                        lane=plan.decision_lane, series=plan.series, category=category(tk), event_ticker=tk.rsplit("-", 1)[0], side=plan.side)
        try:
            placed = runner.place(s, plan.venue, store, shot, reason, whole_contracts(plan.stake, ask))
            filled, cost, note = placed.filled, placed.cost, placed.note
        except KalshiRateLimited as exc:
            note = f"rate limited: {exc}"
        log.info("%s %s %s: %s", plan.label, tk, plan.side, note[:200])
        plan.ctx["live"] = True
        if filled <= 0:
            plan.ctx["miss"] = note[:200]  # a live order that bought nothing is logged as an observe bet at the quote read before it; this says it was not paper
            reason = f"{reason} (live order not filled: {note})"
    store.add_decision(ticker=tk, title="", bid=0.0, ask=ask, model_p=0.0, edge=0.0, action="bet", mode="trade" if filled > 0 else "observe",
                       lane=plan.decision_lane, category=category(tk), side=plan.side, contracts=filled if filled > 0 else plan.paper_contracts,
                       stake_usd=round(cost, 4) if filled > 0 else plan.paper_stake, reason=(note if filled > 0 else reason)[:300],
                       context=json.dumps(plan.ctx), close_time=plan.log_close)
