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


class ColdMemory(RuntimeError):
    """A decision asked for something a background job has not put in memory yet. The decision is skipped, not answered from the database."""


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


def _sized_open(row: Any) -> bool:
    """Store.paper_bets(open_only=True) for a row of the bet book."""
    return (row["action"] == "bet" and row["stake_usd"] is not None and (row["contracts"] or 0) > 0 and (row["lane"] or "moonshot") != "manual"
            and (row["outcome"] or "") not in ("yes", "no") and row["exit_ts"] is None)


class Bankroll:
    """Reads the balance, caching the Kalshi call for a few seconds.

    In the running bot (`served`, 2026-11-19: no database or REST read inside a buy decision) read() answers from memory: the bets still open come from the bet book, the dollars already won or lost and Kalshi's cash and portfolio value are a snapshot that `refresh` takes in a background job, as old as REFRESH_EVERY at most, and read() never takes one itself."""

    TTL = 20.0
    REFRESH_EVERY = 30.0
    served = False  # set by the worker (serve_from_memory)

    def __init__(self, store: Store):
        self.store = store
        self._cached: dict[int, tuple[float, Balance]] = {}
        self._realized: tuple[float, float] | None = None  # (taken at, dollars returned minus staked on finished paper bets)
        self._cash: dict[int, tuple[float, float, float]] = {}  # id(client) -> (taken at, cash, portfolio value)
        self._open_memo: tuple[list, dict[str, float], dict[str, float]] | None = None  # (the bet book it was summed from, open paper stake by lane, open real stake by lane)

    def refresh(self, client: Any, s: Settings) -> None:
        """Background job: the snapshot the decision reads. The database and Kalshi are read here and nowhere in a decision."""
        pnl = paper_pnl(self.store)
        self._realized = (time.time(), sum(v["returned"] - v["staked"] for v in pnl.values()))
        if s.mode == "trade" and getattr(client, "authed", False):
            resp = client.balance()
            cash = float(resp.get("balance_dollars") or (resp.get("balance") or 0) / 100.0)
            self._cash[id(client)] = (time.time(), cash, float(resp.get("portfolio_value") or 0) / 100.0)

    def _open_by_lane(self, book: list) -> tuple[dict[str, float], dict[str, float]]:
        """The open stake of each lane (paper, real), summed once for each bet book and reused until the bet book is replaced (the keeper rebuilds it in the background, and an order rebuilds it at once). A buy decision used to sum every row of the book itself: 15-19 ms of the "balance read" step on the host, 2026-11-21 (user: balance reads should be a background task, not part of the decision)."""
        hit = self._open_memo
        if hit is not None and hit[0] is book:
            return dict(hit[1]), dict(hit[2])
        open_paper: dict[str, float] = {}
        open_real: dict[str, float] = {}
        for r in book:
            if _sized_open(r):
                lane = r["lane"] or "moonshot"
                into = open_real if r["mode"] == "trade" else open_paper
                into[lane] = into.get(lane, 0.0) + r["stake_usd"]
        self._open_memo = (book, open_paper, open_real)
        return dict(open_paper), dict(open_real)

    def _read_served(self, client: Any, s: Settings, live: bool) -> Balance:
        book = self.store._book() or []
        open_paper, open_real = self._open_by_lane(book)
        realized = self._realized[1] if self._realized else 0.0
        equity = s.paper_bankroll_usd + realized
        if equity < s.paper_bankroll_usd:
            equity = s.paper_bankroll_usd  # paper stakes are sized as if the starting stake were intact (see read)
        paper = Balance(max(0.0, equity - sum(open_paper.values())), equity, "paper", open_paper)
        if not live or s.mode != "trade" or not getattr(client, "authed", False):
            return paper
        cash = self._cash.get(id(client))
        if cash is None:
            raise ColdMemory("the account balance has not been read yet (the background refresh has not run)")
        return Balance(cash[1], cash[1] + cash[2], "kalshi", open_real)

    def read(self, client: Any, s: Settings, live: bool = True) -> Balance:
        if self.served:
            return self._read_served(client, s, live)
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
