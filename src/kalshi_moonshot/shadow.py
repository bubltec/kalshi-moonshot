"""Measure markets we do not trade, so a decision to trade them can rest on evidence.

The 15-minute commodity and crypto markets mostly do not pass the lane's return filter: the decided
side sells for 97-99 cents. Whether the cheap side is ever underpriced is an empirical question, and
a model's opinion is the wrong way to answer it. So every review pass records each such market's book
and our model's chance, and when the market settles the row gets its outcome. `shadow_table` then
compares, per time-left and price bucket, the market price, our chance, and how often YES really won.

Nothing here sizes, orders, or scores a bet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .commodities import CommodityError, CommodityPricer
from .config import Settings
from .store import Store
from .strategy import fee_per_contract

MIN_GAP_SECONDS = 45.0  # one snapshot per market per this long
TIME_BUCKETS = ((0.0, 2.0), (2.0, 4.0), (4.0, 7.0), (7.0, 11.0), (11.0, 16.0))
PRICE_BUCKETS = (
    (0.00, 0.03), (0.03, 0.10), (0.10, 0.25), (0.25, 0.75), (0.75, 0.90), (0.90, 0.97), (0.97, 1.01),
)
THIN = 30  # buckets with fewer markets than this are flagged: too few to conclude anything


def snapshot(
    store: Store, s: Settings, markets: list[dict[str, Any]], suffix: str,
    pricer: CommodityPricer | None, now: datetime | None = None,
) -> int:
    """Record the book of each market in a series ending with `suffix`. Returns rows written."""
    now = now or datetime.now(UTC)
    wrote = 0
    for m in markets:
        series = m.get("series_ticker") or str(m.get("event_ticker", "")).split("-")[0]
        if not suffix or not series.endswith(suffix) or m.get("status") not in {"active", "open"}:
            continue
        if store.shadowed_since(m["ticker"], now.timestamp() - MIN_GAP_SECONDS):
            continue
        try:
            minutes = (datetime.fromisoformat(m["close_time"]) - now).total_seconds() / 60.0
            bid, ask = float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"])
        except (KeyError, TypeError, ValueError):
            continue
        if minutes <= 0.0:
            continue
        model_p, note = None, ""
        if pricer is not None:
            try:
                model_p, note = pricer.price_markets([m], s, now)[m["ticker"]]
            except (CommodityError, KeyError) as exc:
                note = f"unpriced: {exc}"
        store.add_shadow(
            ticker=m["ticker"], series=series, minutes_left=minutes, yes_bid=bid, yes_ask=ask,
            model_p=model_p, strike=m.get("floor_strike"), note=note[:200],
        )
        wrote += 1
    return wrote


@dataclass(frozen=True)
class Cell:
    minutes: tuple[float, float]
    price: tuple[float, float]
    n: int
    yes_rate: float  # how often YES actually won
    market: float  # mean YES mid
    model: float | None  # mean model chance of YES
    roi_yes: float  # buying YES at the ask, net of fee
    roi_no: float  # buying NO at 1 - bid, net of fee

    @property
    def thin(self) -> bool:
        return self.n < THIN


def _roi(wins: float, costs: list[float]) -> float:
    spent = sum(costs)
    return (wins - spent) / spent if spent > 0 else float("nan")


def shadow_table(rows: list[Any]) -> list[Cell]:
    """Calibration cells. Each market counts once per time bucket (its latest snapshot there)."""
    latest: dict[tuple[str, int], Any] = {}
    for r in rows:
        for i, (lo, hi) in enumerate(TIME_BUCKETS):
            if lo <= r["minutes_left"] < hi:
                latest[(r["ticker"], i)] = r  # rows arrive oldest first, so the last one wins
    cells: list[Cell] = []
    for ti, tb in enumerate(TIME_BUCKETS):
        in_t = [r for (_, i), r in latest.items() if i == ti]
        for pb in PRICE_BUCKETS:
            grp = [r for r in in_t if pb[0] <= (r["yes_bid"] + r["yes_ask"]) / 2 < pb[1]]
            if not grp:
                continue
            yes = [1.0 if r["outcome"] == "yes" else 0.0 for r in grp]
            asks = [r["yes_ask"] + fee_per_contract(r["yes_ask"]) for r in grp if 0 < r["yes_ask"] < 1]
            nocost = [(1 - r["yes_bid"]) + fee_per_contract(1 - r["yes_bid"]) for r in grp if 0 < r["yes_bid"] < 1]
            wins_yes = sum(1.0 if r["outcome"] == "yes" else 0.0 for r in grp if 0 < r["yes_ask"] < 1)
            wins_no = sum(1.0 if r["outcome"] == "no" else 0.0 for r in grp if 0 < r["yes_bid"] < 1)
            priced = [r["model_p"] for r in grp if r["model_p"] is not None]
            cells.append(Cell(
                tb, pb, len(grp), sum(yes) / len(yes),
                sum((r["yes_bid"] + r["yes_ask"]) / 2 for r in grp) / len(grp),
                sum(priced) / len(priced) if priced else None,
                _roi(wins_yes, asks), _roi(wins_no, nocost),
            ))
    return cells
