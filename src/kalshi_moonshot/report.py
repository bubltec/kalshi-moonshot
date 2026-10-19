"""Does the model beat the market? Scores settled decisions both ways.

Brier score: mean squared error of a probability against what happened (lower is better).
If the model's Brier is not clearly below the market's, there is no edge to trade.

The bot re-scores the same market every scan, so each market is counted once, at the first
time the model rated it. Without that, a few markets look like dozens of independent results.
"""

from __future__ import annotations

from typing import Any

from .strategy import fee_per_contract


def brier(pairs: list[tuple[float, int]]) -> float:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else float("nan")


def _get(r: Any, key: str, default: Any = None) -> Any:
    try:
        return r[key]
    except (KeyError, IndexError):
        return default


def _yes(r: Any) -> int:
    return 1 if r["outcome"] == "yes" else 0


def first_per_market(rows: list[Any]) -> list[Any]:
    """Earliest rated decision per ticker. Rows without a ticker are all kept."""
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["model_p"] is None:
            continue
        ticker = _get(r, "ticker")
        if ticker is not None:
            if ticker in seen:
                continue
            seen.add(ticker)
        out.append(r)
    return out


def summarize(rows: list[Any]) -> dict[str, Any]:
    """rows: settled decisions with model_p, ask, outcome, action."""
    rated = first_per_market(rows)
    model = brier([(r["model_p"], _yes(r)) for r in rated])
    market = brier([(r["ask"], _yes(r)) for r in rated])
    bets = [r for r in rated if r["action"] == "bet"]
    held = [r for r in bets if _get(r, "exit_price") is None]
    stopped = [r for r in bets if _get(r, "exit_price") is not None]

    def pnl_one(r: Any) -> float:
        entry = r["ask"] + fee_per_contract(r["ask"])
        if _get(r, "exit_price") is not None:
            return float(r["exit_price"]) - fee_per_contract(float(r["exit_price"])) - entry
        return _yes(r) - entry

    pnl = sum(pnl_one(r) for r in bets)
    cost = sum(r["ask"] + fee_per_contract(r["ask"]) for r in bets)
    return {
        "settled": len(rated),
        "model_brier": model,
        "market_brier": market,
        "model_beats_market": bool(rated) and model < market,
        "bets": len(bets),
        "bet_wins": sum(_yes(r) for r in held),
        "stop_losses": len(stopped),
        "bet_pnl_per_contract_set": pnl,
        "bet_return_pct": (pnl / cost * 100.0) if cost else float("nan"),
    }


def by_category(rows: list[Any]) -> dict[str, dict[str, Any]]:
    """Scorecard per Kalshi category, so one bad category does not hide a good one."""
    groups: dict[str, list[Any]] = {}
    for r in rows:
        groups.setdefault(_get(r, "category") or "unknown", []).append(r)
    return {cat: summarize(group) for cat, group in sorted(groups.items())}


def by_lane(rows: list[Any]) -> dict[str, dict[str, Any]]:
    """Separate scorecards, because a steady-lane win rate and a moonshot one mean different things."""
    lanes: dict[str, list[Any]] = {}
    for r in rows:
        lanes.setdefault(_get(r, "lane") or "moonshot", []).append(r)
    return {lane: summarize(group) for lane, group in sorted(lanes.items())}
