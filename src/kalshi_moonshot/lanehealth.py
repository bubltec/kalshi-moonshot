"""Hourly check that every live lane is doing something, and that no live bet is left unsettled (2026-11-06).

Two things went unnoticed for hours on 11-06: treasury_favorite inherited a gate it could never pass (the yield markets have no index feed, so every evaluation was skipped as "index history too short"), and a treasury bet sat unsettled for a day on Kalshi's side. The user saw both before the bot said anything.
A live lane is *silent* when, over the look-back, it was evaluated at least MIN_EVALS times, bought nothing, and one skip reason covers at least SHARE of its evaluations. That is not always a fault (a multi-scale lane waits for its variety pair), so a finding names the reason and the reader decides. A live bet is *stale* when its market closed more than STALE_HOURS ago and it has no result and no exit.
Findings are logged as warnings the first time they appear and rewritten to DATA_DIR/lane_health.json each run. Nothing here changes a setting or places an order."""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("moonshot")

EVERY = 3600.0
LOOKBACK_HOURS = 4.0
MIN_EVALS = 20
SHARE = 0.90
STALE_HOURS = 2.0

_checked_at = 0.0
_reported: set[str] = set()


def _reason_key(reason: str | None) -> str:
    """A skip reason without its bracketed tags and numbers, so the same rule counts as one reason."""
    text = re.sub(r"\[[^\]]*\]\s*", "", reason or "")
    return re.sub(r"[-+]?\d+(\.\d+)?", "#", text)[:80]


def silent_lanes(rows: list[Any], live_lanes: set[str]) -> list[dict[str, Any]]:
    """Lanes in `rows` (decisions: lane, action, reason) that were evaluated often, bought nothing, and skipped for one reason almost every time."""
    evals: Counter[str] = Counter()
    bets: Counter[str] = Counter()
    reasons: dict[str, Counter[str]] = {}
    for r in rows:
        lane = r["lane"]
        if lane not in live_lanes:
            continue
        evals[lane] += 1
        if r["action"] == "bet":
            bets[lane] += 1
        else:
            reasons.setdefault(lane, Counter())[_reason_key(r["reason"])] += 1
    out = []
    for lane, n in sorted(evals.items()):
        if n < MIN_EVALS or bets[lane] > 0 or lane not in reasons:
            continue
        reason, count = reasons[lane].most_common(1)[0]
        if count / n >= SHARE:
            out.append({"kind": "silent", "lane": lane, "evaluations": n, "share": round(count / n, 2), "reason": reason})
    return out


def stale_bets(rows: list[Any], now: float) -> list[dict[str, Any]]:
    """Live bets (ticker, lane, close_time as ISO text) whose market closed over STALE_HOURS ago and that still have no result."""
    out = []
    for r in rows:
        try:
            closed = datetime.fromisoformat(str(r["close_time"])).timestamp()
        except (TypeError, ValueError):
            continue
        if now - closed >= STALE_HOURS * 3600.0:
            out.append({"kind": "stale", "lane": r["lane"], "ticker": r["ticker"], "hours": round((now - closed) / 3600.0, 1)})
    return out


def check(s: Any, store: Any, live_lanes: set[str], now: float | None = None) -> list[dict[str, Any]]:
    """Run both checks (at most once an hour), log what is new, write the report. Returns the findings."""
    global _checked_at
    now = time.time() if now is None else now
    if now - _checked_at < EVERY:
        return []
    _checked_at = now
    since = now - LOOKBACK_HOURS * 3600.0
    evals = store.db.execute("SELECT lane, action, reason FROM decisions WHERE mode='trade' AND ts>?", (since,)).fetchall()
    open_bets = store.db.execute(
        "SELECT lane, ticker, close_time FROM decisions WHERE mode='trade' AND action='bet' AND stake_usd>0 AND outcome IS NULL AND exit_price IS NULL AND ts>?", (now - 7 * 86400.0,)
    ).fetchall()
    found = silent_lanes(list(evals), live_lanes) + stale_bets(list(open_bets), now)
    keys = set()
    for f in found:
        key = f"{f['kind']}:{f['lane']}:{f.get('ticker', '')}"
        keys.add(key)
        if key in _reported:
            continue
        if f["kind"] == "silent":
            log.warning("lane health: %s bought nothing in %.0f h, %d evaluations, %.0f%% skipped for: %s", f["lane"], LOOKBACK_HOURS, f["evaluations"], 100 * f["share"], f["reason"])
        else:
            log.warning("lane health: %s bet on %s is unsettled %.1f h after the market closed (Kalshi has not set the result)", f["lane"], f["ticker"], f["hours"])
    _reported.intersection_update(keys)
    _reported.update(keys)
    try:
        (Path(s.data_dir) / "lane_health.json").write_text(json.dumps({"at": round(now), "findings": found}, indent=1))
    except OSError:
        log.debug("lane health: could not write the report", exc_info=True)
    return found
