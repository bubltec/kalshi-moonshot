"""How much to stake: the odds and the edge decide, scaled to the balance we actually have.

Fractional Kelly. For a contract that costs c (price plus fee) and pays $1 with probability p, the
full-Kelly share of the bankroll is (p - c) / (1 - c). A cheap long shot with a real edge gets a
small share (few wins, large payouts); a near-certain favorite gets a large one. Full Kelly is
too aggressive for estimates that are not exact, so only `kelly_fraction` of it is used, and two
caps per lane keep any one bet, and any one kind of bet, from ruining the balance:

  max_bet_pct       one bet, as a share of equity (small for moonshots: many cheap tickets)
  max_exposure_pct  everything the lane has open at once, as a share of equity

Equity is cash plus what is tied up in open bets, so stakes grow as the balance grows and shrink as
it shrinks. Nothing here ever exceeds available cash or the hard dollar ceilings.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .lanes import LaneSpec
from .strategy import contracts_for_budget, fee_per_contract, order_fee


@dataclass(frozen=True)
class Sizing:
    contracts: int
    cost_usd: float  # price plus fee for all contracts
    why: str

    @property
    def ok(self) -> bool:
        return self.contracts > 0


def kelly_share(p: float, ask: float) -> float:
    """Full-Kelly share of the bankroll for a $1-payout contract. Zero when there is no edge."""
    c = ask + fee_per_contract(ask)
    if c >= 1.0 or p <= c:
        return 0.0
    return (p - c) / (1.0 - c)


def size_bet(
    spec: LaneSpec, s: Settings, p: float, ask: float,
    available: float, equity: float, lane_exposure: float,
) -> Sizing:
    """Contracts to buy now. contracts == 0 comes with the reason it was not sized."""
    full = kelly_share(p, ask)
    if full <= 0.0 and not spec.fixed_stake:
        return Sizing(0, 0.0, "no edge after the fee at this chance and price")
    ceilings = [s.max_usd_per_trade]
    if spec.usd_per_trade:
        ceilings.append(spec.usd_per_trade)
    # A fixed-stake lane bets the same dollars every time; the odds do not scale it.
    wanted = spec.fixed_stake or min(equity * s.kelly_fraction * full, equity * spec.max_bet_pct)
    stake = min(wanted, equity * spec.max_exposure_pct - lane_exposure, available, *ceilings)
    if stake <= 0.0:
        room = equity * spec.max_exposure_pct - lane_exposure
        return Sizing(0, 0.0, "no room left" if room <= 0 else "no cash available")
    count = contracts_for_budget(stake, ask)
    if count <= 0:
        return Sizing(0, 0.0, f"${stake:.2f} stake is under the cost of one contract")
    cost = ask * count + order_fee(ask, count)
    how = "fixed stake" if spec.fixed_stake else f"{full:.0%} Kelly, {s.kelly_fraction:.0%} of it"
    return Sizing(count, cost, f"stake ${cost:.2f} ({how})")
