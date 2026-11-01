"""Look at every open position on a schedule and save what the market looked like, so a later replay can ask what selling at that moment would have done.

At each mark (seconds before the close: 240, 210, 180 ... 30, then 10) the watcher saves one row per open position into position_checks: the bid it could be sold for, the other side's ask, how far the index is
through the strike against the position, the strike-through watch's own state, and the full factor snapshot. Nothing here sells or changes a rule; the strike-through sale keeps its own 60-second window.
A mark that is missed by more than LATE seconds (a position found late, a stalled thread) is skipped, never filled in afterwards with a different moment.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("moonshot")

MARKS = (240, 210, 180, 150, 120, 90, 60, 30, 10)
LATE = 10.0


def due(left: float, seen: set[int]) -> int | None:
    """The mark to record now, or None. `seen` holds the marks already handled for this position and is updated: every mark passed is marked handled, and only the latest is recorded, and only if it was reached
    within LATE seconds."""
    if left <= 0:
        return None
    passed = [m for m in MARKS if m >= left and m not in seen]
    if not passed:
        return None
    seen.update(passed)
    mark = min(passed)
    return mark if mark - left <= LATE else None


def side_through(side: str, strike: float, value: float) -> float:
    """Signed share of the strike the index is through against this side: positive on the losing side."""
    if strike <= 0 or value <= 0:
        return 0.0
    return (strike - value) / strike if side == "yes" else (value - strike) / strike


def build_row(t: Any, mark: int, now: float, spot: tuple[float, float] | None, features: str) -> dict[str, Any]:
    value = spot[1] if spot else 0.0
    return {
        "ticker": t.ticker, "lane": t.lane, "mark": mark, "side": t.side, "ts": now, "left_s": round(t.close_ts - now, 2), "close_ts": t.close_ts, "entry": t.entry,
        "bid": t.cur_bid or None, "other_ask": t.cur_other_ask or None, "strike": t.strike or None, "spot": value or None, "spot_age": round(now - spot[0], 2) if spot else None,
        "through": round(side_through(t.side, t.strike, value), 6) if spot and t.strike else None, "through_on": int(bool(t.through_on)), "through_kept": int(bool(t.through_kept)), "features": features,
    }


def close_iso(close_ts: float) -> str:
    return datetime.fromtimestamp(close_ts, UTC).isoformat()


def record(store: Any, row: dict[str, Any]) -> None:
    cols = list(row)
    store.db.execute(f"INSERT OR IGNORE INTO position_checks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(row[c] for c in cols))
    store.db.commit()


def snapshot(s: Any, t: Any) -> str:
    from . import factors

    try:
        return factors.snapshot_now(s, t.ticker, t.side, close_iso(t.close_ts), "", t.strike or None) or json.dumps({})
    except Exception:
        log.debug("checkins: no snapshot for %s", t.ticker, exc_info=True)
        return ""
