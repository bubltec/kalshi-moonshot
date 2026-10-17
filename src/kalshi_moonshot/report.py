"""Does the model beat the market? Scores settled decisions both ways.

Brier score: mean squared error of a probability against what happened (lower is better).
If the model's Brier is not clearly below the market's, there is no edge to trade.
"""

from __future__ import annotations

from typing import Any

from .strategy import fee_per_contract


def brier(pairs: list[tuple[float, int]]) -> float:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else float("nan")


def summarize(rows: list[Any]) -> dict[str, Any]:
    """rows: settled decisions with model_p, ask, outcome, action."""
    rated = [r for r in rows if r["model_p"] is not None]
    y = lambda r: 1 if r["outcome"] == "yes" else 0
    model = brier([(r["model_p"], y(r)) for r in rated])
    market = brier([(r["ask"], y(r)) for r in rated])
    bets = [r for r in rated if r["action"] == "bet"]
    # One contract per bet, paid ask + fee, receives $1 on yes.
    pnl = sum(y(r) - r["ask"] - fee_per_contract(r["ask"]) for r in bets)
    cost = sum(r["ask"] + fee_per_contract(r["ask"]) for r in bets)
    return {
        "settled": len(rated),
        "model_brier": model,
        "market_brier": market,
        "model_beats_market": bool(rated) and model < market,
        "bets": len(bets),
        "bet_wins": sum(y(r) for r in bets),
        "bet_pnl_per_contract_set": pnl,
        "bet_return_pct": (pnl / cost * 100.0) if cost else float("nan"),
    }
