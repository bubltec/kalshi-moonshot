"""How deep the queue was ahead of each resting bid, and a cutoff from the bids that filled (2026-11-16, user: "build that").

Every resting bid appends one row to data/queue_bids.jsonl: the shares ahead of it when it was placed (read from the websocket book, ticks.books) and whether it filled. A lane with `buy_rest_queue_quantile` skips a bid whose queue is deeper than that quantile of the queue ahead of ITS OWN filled bids over the last `buy_rest_queue_hours` (rule 2: a cutoff is a quantile of recent history, not a fixed share count; rule 3: until `MIN_FILLS` filled bids are on record there is no cutoff and no bid is skipped). The cutoff is per lane, never per coin."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

MIN_FILLS = 10  # filled bids needed before a quantile means anything; fewer: no cutoff
_LOCK = threading.Lock()
_ROWS: dict[str, list[tuple[float, float, bool]]] = {}  # lane -> (ts, shares ahead, filled)
_LOADED: set[str] = set()


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / "queue_bids.jsonl"


def _load(data_dir: Path, now: float, hours: float) -> None:
    key = str(data_dir)
    if key in _LOADED:
        return
    _LOADED.add(key)
    try:
        with open(_path(data_dir)) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if now - float(r["ts"]) <= max(hours, 12.0) * 3600.0:
                    _ROWS.setdefault(str(r["lane"]), []).append((float(r["ts"]), float(r["ahead"]), bool(r["filled"])))
    except OSError:
        pass


def record(data_dir: Path, lane: str, ticker: str, ahead: float, filled: bool, now: float | None = None) -> None:
    """One resting bid's queue and result. Never raises."""
    now = time.time() if now is None else now
    try:
        with _LOCK:
            _load(data_dir, now, 12.0)
            _ROWS.setdefault(lane, []).append((now, float(ahead), bool(filled)))
            with open(_path(data_dir), "a") as f:
                f.write(json.dumps({"ts": round(now, 1), "lane": lane, "ticker": ticker, "ahead": round(float(ahead), 2), "filled": bool(filled)}) + "\n")
    except (OSError, ValueError):
        pass


def cutoff(data_dir: Path, lane: str, quantile: float, hours: float, now: float | None = None) -> float | None:
    """The `quantile` of the queue ahead of this lane's filled bids in the last `hours`, or None (fewer than MIN_FILLS filled bids on record)."""
    now = time.time() if now is None else now
    with _LOCK:
        _load(data_dir, now, hours)
        got = sorted(a for ts, a, ok in _ROWS.get(lane, []) if ok and now - ts <= hours * 3600.0)
    if len(got) < MIN_FILLS:
        return None
    pos = min(1.0, max(0.0, quantile)) * (len(got) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(got) - 1)
    return got[lo] + (got[hi] - got[lo]) * (pos - lo)


def reset() -> None:
    with _LOCK:
        _ROWS.clear()
        _LOADED.clear()
