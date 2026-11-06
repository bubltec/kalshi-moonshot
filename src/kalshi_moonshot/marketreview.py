"""Per-market review in shadow mode, hourly (2026-11-06).

User: every market has its own curve and variance, and the settings should adapt to each market from its own recent results. The old hourly review and tuning jobs are retired; this is their replacement, and it applies nothing yet.
Each hour, for every 15-minute series with settled live bets in the last 72 h, it reads the per-bet return (profit over stake) and proposes one of:
  stand_aside  the market's return is clearly negative (t <= STAND_ASIDE_T on at least MIN_BETS bets): stake 0
  reduce       leaning negative (t <= REDUCE_T): stake x REDUCE_FACTOR
  press        clearly positive (t >= PRESS_T): stake x PRESS_FACTOR
  keep         otherwise, or too few bets
where t = mean return / its standard error. The market's type (marketprofile) is written beside it so a later pass can see whether types behave alike.
A proposal is then scored against the bets actually placed in the hour after it: what the stakes would have returned under the proposal against what they did return. Both are logged as running totals, so by the time anyone turns this on the evidence is already there.
Lines go to DATA_DIR/market_review.jsonl: kind "proposal" (one per series per hour) and kind "score" (one per proposal, written once its hour has passed). Nothing here changes a lane, a stake or an order."""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("moonshot")

EVERY = 3600.0
LOOKBACK_HOURS = 72.0
MIN_BETS = 15
STAND_ASIDE_T = -1.5
REDUCE_T = -0.75
PRESS_T = 1.5
REDUCE_FACTOR = 0.5
PRESS_FACTOR = 1.25

_checked_at = 0.0


def bet_return(row: Any) -> tuple[float, float] | None:
    """(stake, profit) of a settled live bet row, or None when it has no result or exit yet. An exit sells at exit_price; otherwise the contracts pay 1 on a win."""
    stake = float(row["stake_usd"] or 0.0)
    ask = float(row["ask"] or 0.0)
    if stake <= 0 or ask <= 0:
        return None
    n = float(row["contracts"] or 0.0) or stake / ask
    if row["exit_price"] is not None:
        value = n * float(row["exit_price"])
    elif row["outcome"] in ("yes", "no"):
        value = n if row["outcome"] == row["side"] else 0.0
    else:
        return None
    return stake, value - stake


def propose(returns: list[float]) -> dict[str, Any]:
    """The shadow action for one market from its per-bet returns (profit / stake, oldest first)."""
    n = len(returns)
    if n < MIN_BETS:
        return {"action": "keep", "factor": 1.0, "n": n, "t": None}
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    se = math.sqrt(var / n)
    t = mean / se if se > 0 else math.copysign(99.0, mean) if mean else 0.0  # every bet returned the same: as sure as it gets
    if t <= STAND_ASIDE_T:
        action, factor = "stand_aside", 0.0
    elif t <= REDUCE_T:
        action, factor = "reduce", REDUCE_FACTOR
    elif t >= PRESS_T:
        action, factor = "press", PRESS_FACTOR
    else:
        action, factor = "keep", 1.0
    return {"action": action, "factor": factor, "n": n, "t": round(t, 2), "mean": round(mean, 4)}


def score(factor: float, bets: list[tuple[float, float]]) -> dict[str, float]:
    """What a stake factor would have made on bets (stake, profit) already placed: profit scales with the stake. Returns actual and shadow dollars."""
    actual = sum(p for _, p in bets)
    return {"bets": float(len(bets)), "actual": round(actual, 4), "shadow": round(actual * factor, 4)}


def _read(path: Path) -> list[dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def run(s: Any, store: Any, types: dict[str, dict[str, Any]] | None = None, now: float | None = None) -> list[dict[str, Any]]:
    """The hourly pass: score the proposals whose hour is over, then write a new proposal per series. Returns the new proposals."""
    global _checked_at
    now = time.time() if now is None else now
    if now - _checked_at < EVERY:
        return []
    _checked_at = now
    path = Path(s.data_dir) / "market_review.jsonl"
    history = _read(path)
    rows = store.db.execute(
        "SELECT ts, ticker, side, ask, contracts, stake_usd, outcome, exit_price FROM decisions WHERE mode='trade' AND action='bet' AND stake_usd>0 AND ts>? ORDER BY ts", (now - LOOKBACK_HOURS * 3600.0,)
    ).fetchall()
    by_series: dict[str, list[tuple[float, float, float]]] = {}
    for r in rows:
        got = bet_return(r)
        if got:
            by_series.setdefault(str(r["ticker"]).split("-")[0], []).append((float(r["ts"]), got[0], got[1]))
    scored = {(h["series"], h["ts"]) for h in history if h.get("kind") == "score"}
    out: list[dict[str, Any]] = []
    for h in history:
        if h.get("kind") != "proposal" or (h["series"], h["ts"]) in scored or now - h["ts"] < EVERY:
            continue
        window = [(st, p) for t, st, p in by_series.get(h["series"], []) if h["ts"] <= t < h["ts"] + EVERY]
        out.append({"kind": "score", "series": h["series"], "ts": h["ts"], "action": h["action"], "factor": h["factor"], **score(h["factor"], window)})
    for series, items in sorted(by_series.items()):
        if not series.endswith("15M"):
            continue
        returns = [p / st for _, st, p in items]
        prop = propose(returns)
        out.append({"kind": "proposal", "series": series, "ts": round(now), "type": (types or {}).get(series, {}).get("type"), **prop})
    try:
        with path.open("a") as fh:
            for line in out:
                fh.write(json.dumps(line) + "\n")
    except OSError:
        log.debug("market review: could not write %s", path, exc_info=True)
    done = [h for h in history + out if h.get("kind") == "score" and now - h["ts"] <= 86400.0]
    acted = [h for h in done if h["action"] != "keep" and h["bets"] > 0]
    log.info(
        "market review (shadow): %d proposals, %d acted-on and scored in 24 h: actual $%.2f vs shadow $%.2f",
        sum(1 for h in out if h["kind"] == "proposal" and h["action"] != "keep"), len(acted), sum(h["actual"] for h in acted), sum(h["shadow"] for h in acted),
    )
    return out
