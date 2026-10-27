"""The balance sizing works from.

Trading with credentials: Kalshi's own available balance (cash not tied up in open positions) and
portfolio value. Otherwise, a paper balance: PAPER_BANKROLL_USD plus what the paper bets have
returned so far, less what is still riding on open ones. It compounds the way a real one would.

A paper bet's stake is saved on the decision that made it (contracts, stake_usd), so the paper
balance is always recomputable from the database and survives restarts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .store import Store
from .strategy import fee_per_contract


@dataclass(frozen=True)
class Balance:
    available: float  # cash we can stake now
    equity: float  # cash plus open bets: what percentages are taken of
    source: str  # "kalshi" or "paper"
    open_by_lane: dict[str, float]


def _won(row: Any) -> bool:
    return row["outcome"] == (row["side"] or "yes")


def paper_pnl(store: Store, live: bool = False) -> dict[str, dict[str, float]]:
    """Per lane: bets finished, wins, dollars returned minus staked."""
    out: dict[str, dict[str, float]] = {}
    for r in store.paper_bets(open_only=False, live=live):
        lane = r["lane"] or "moonshot"
        agg = out.setdefault(lane, {"n": 0, "wins": 0, "staked": 0.0, "returned": 0.0})
        agg["n"] += 1
        agg["staked"] += r["stake_usd"]
        if r["exit_price"] is not None:
            agg["returned"] += r["contracts"] * (r["exit_price"] - fee_per_contract(r["exit_price"]))
        elif _won(r):
            agg["wins"] += 1
            agg["returned"] += r["contracts"]
    return out


def paper_balance(store: Store, s: Settings) -> Balance:
    open_by_lane: dict[str, float] = {}
    for r in store.paper_bets(open_only=True):
        lane = r["lane"] or "moonshot"
        open_by_lane[lane] = open_by_lane.get(lane, 0.0) + r["stake_usd"]
    pnl = paper_pnl(store)
    realized = sum(v["returned"] - v["staked"] for v in pnl.values())
    open_total = sum(open_by_lane.values())
    equity = s.paper_bankroll_usd + realized
    return Balance(max(0.0, equity - open_total), equity, "paper", open_by_lane)


class Bankroll:
    """Reads the balance, caching the Kalshi call for a few seconds."""

    TTL = 20.0

    def __init__(self, store: Store):
        self.store = store
        self._cached: dict[int, tuple[float, Balance]] = {}

    def read(self, client: Any, s: Settings, live: bool = True) -> Balance:
        paper = paper_balance(self.store, s)
        if paper.equity < s.paper_bankroll_usd:
            # Paper trading exists to collect results. Sizing off a bankroll that paper losses have eaten would starve every paper lane
            # (a $1 fixed stake cannot fit under 10% of $4), so paper stakes are sized as if the starting stake were intact.
            paper = Balance(max(0.0, s.paper_bankroll_usd - sum(paper.open_by_lane.values())), s.paper_bankroll_usd, "paper", paper.open_by_lane)
        if not live or s.mode != "trade" or not getattr(client, "authed", False):
            return paper
        hit = self._cached.get(id(client))
        if hit and time.time() - hit[0] < self.TTL:
            return hit[1]
        resp = client.balance()
        cash = float(resp.get("balance_dollars") or (resp.get("balance") or 0) / 100.0)
        held = float(resp.get("portfolio_value") or 0) / 100.0
        real_open: dict[str, float] = {}
        for r in self.store.paper_bets(open_only=True, live=True):
            lane = r["lane"] or "moonshot"
            real_open[lane] = real_open.get(lane, 0.0) + r["stake_usd"]
        real = Balance(cash, cash + held, "kalshi", real_open)
        self._cached[id(client)] = (time.time(), real)
        return real
