"""After a stop-loss sells a 15-minute position, ask the model whether the OTHER side is now worth buying.

A stop means the market moved hard against us. Sometimes that move is the new truth (the other side is now the favorite) and sometimes it is noise that reverts. Rather than
guess, the stop leaves a request (store.add_flip); the worker then shows the local model the window's shape so far (the same prompt the llm_shape lane uses), weighs its
chance against the other side's price like any lane, and records the answer as a paper bet or a pass in the `stop_flip` lane. It never trades: the lane is not live, and
whether it deserves to be is for the recorded results to say.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from . import runner
from .config import Settings
from .kalshi import KalshiError
from .lanes import SHAPE, spec_for
from .store import Store
from .strategy import lane_candidates, lane_reject_reason, side_view

log = logging.getLogger("moonshot")

LANE = "stop_flip"
WINDOW_SUFFIX = "15M"  # the shape prompt reads fixed 15-minute windows


def request(s: Settings, store: Store, ticker: str, stopped_side: str, lane: str, stake_usd: float, why: str) -> bool:
    """Called when a stop has just sold. True when a second look was queued."""
    if not s.flip_after_stop or lane == LANE or not why.startswith("stop-loss") or not ticker.split("-")[0].endswith(WINDOW_SUFFIX):
        return False
    return store.add_flip(ticker, stopped_side, lane, stake_usd)


def process(s: Settings, client: Any, store: Store, estimators: dict, now_ts: float | None = None) -> list[str]:
    """Work through the pending requests once. Returns a line for each that was settled."""
    now_ts = time.time() if now_ts is None else now_ts
    now = datetime.fromtimestamp(now_ts, UTC)
    spec = spec_for(s, LANE)
    estimator = estimators.get(SHAPE)
    lines: list[str] = []

    def done(row, status: str, note: str) -> None:
        store.finish_flip(row["id"], status, note)
        lines.append(f"flip {row['ticker']}: {status}. {note}")

    for row in store.pending_flips():
        if now_ts - row["ts"] > s.flip_wait_seconds:
            done(row, "expired", "the model did not answer in time")
            continue
        if estimator is None:
            done(row, "expired", "no model available")
            continue
        try:
            m = client.market(row["ticker"])
        except KalshiError as exc:
            log.debug("flip: could not read %s (%s)", row["ticker"], exc)
            continue
        if m.get("result") in {"yes", "no"}:
            done(row, "closed", "the market has settled")
            continue
        m.setdefault("series_ticker", row["ticker"].split("-")[0])
        other = "no" if row["stopped_side"] == "yes" else "yes"
        shots = [x for x in lane_candidates([m], spec, s, now) if x.side == other]
        if not shots:
            done(row, "filtered", lane_reject_reason(side_view(m, other), spec, s, now) or "the other side is not a candidate")
            continue
        if not estimator.screen(shots, [m], spec, s, now):
            continue  # the model has been asked; look again shortly
        out = runner.evaluate(s, client, None, store, shots[0], estimators=estimators, raise_model_errors=False)
        done(row, "done", f"{out.action} {other} @ {shots[0].ask:.2f}: {out.note[:160]}")
    return lines
