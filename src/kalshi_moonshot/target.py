"""Paper test of a resting sell at a multiple of the price paid, on the bot's own 15-minute bets.

Nothing here places or changes an order. For each finished 15-minute bet it asks: had a sell been resting at
`mult` times the entry price from the moment of the bet, would a minute's best bid have reached it before the
bet ended? If so the bet is re-scored as sold there (fee included); otherwise it keeps its real result.
Minute candles carry the best bid of each minute, which is what a resting order could have caught. A touch
within the same minute as the entry is not counted, since the order of events inside a minute is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass

from .store import Store
from .strategy import fee_per_contract


@dataclass(frozen=True)
class Outcome:
    ticker: str
    side: str
    entry: float
    stake: float
    actual: float  # real (or paper) result in dollars
    with_target: float
    hit: bool


def _actual(r) -> float:
    if r["exit_price"] is not None:
        return r["contracts"] * (r["exit_price"] - fee_per_contract(r["exit_price"])) - r["stake_usd"]
    return (float(r["contracts"]) if r["outcome"] == (r["side"] or "yes") else 0.0) - r["stake_usd"]


def compare(store: Store, mult: float, suffix: str = "15M", live: bool | None = None, lane: str | None = None) -> tuple[list[Outcome], int]:
    """Returns the scored bets and how many finished bets had no minute candles to test against."""
    rows = store.db.execute(
        """SELECT ticker, lane, side, mode, contracts, stake_usd, ask, outcome, exit_price, exit_ts, ts
           FROM decisions
           WHERE action='bet' AND stake_usd IS NOT NULL AND contracts > 0 AND ask > 0
             AND (COALESCE(outcome,'') IN ('yes','no') OR exit_ts IS NOT NULL)"""
    ).fetchall()
    out: list[Outcome] = []
    missing = 0
    for r in rows:
        if not r["ticker"].split("-")[0].endswith(suffix) or (live is not None and (r["mode"] == "trade") != live):
            continue
        if lane is not None and (r["lane"] or "moonshot") != lane:
            continue
        side = r["side"] or "yes"
        entry = float(r["ask"])
        target = entry * mult
        candles = store.db.execute(
            "SELECT end_ts, bid_hi, ask_lo FROM candles WHERE ticker=? ORDER BY end_ts", (r["ticker"],)
        ).fetchall()
        if not candles:
            missing += 1
            continue
        end = float(r["exit_ts"]) if r["exit_ts"] is not None else float("inf")
        hit = False
        if target < 1.0:
            for c in candles:
                if c["end_ts"] <= r["ts"] + 60 or c["end_ts"] > end:
                    continue
                best = c["bid_hi"] if side == "yes" else (None if c["ask_lo"] is None else 1.0 - c["ask_lo"])
                if best is not None and best >= target:
                    hit = True
                    break
        actual = _actual(r)
        sold = r["contracts"] * (target - fee_per_contract(target)) - r["stake_usd"]
        out.append(Outcome(r["ticker"], side, entry, float(r["stake_usd"]), actual, sold if hit else actual, hit))
    return out, missing
