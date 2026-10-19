"""Hourly review of the scorecard and the settled-case index.

It only tightens settings, and only when a slice of the report is large enough to be clear.
A weak lane stays on: a few bad settled cases are not a reason to drop the whole book.
It cannot turn trading on or raise a risk cap. Undo a change by deleting that key from
data/tuning.json (or the whole file).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any

from . import report
from .config import Settings, read_tuning, tuning_path

# Moonshot edge rises only when the model is this far behind the market.
LANE_BRIER_GAP = 0.08
CATEGORY_MIN_SETTLED = 20
CATEGORY_BRIER_GAP = 0.10
# Sports stays in the scan. The user wants that book even when its settled Brier is behind the market.
KEEP_CATEGORIES = frozenset({"sports"})
# Raise the moonshot edge only after enough bets have lost money.
EDGE_MIN_BETS = 10
EDGE_STEP = 0.02
EDGE_CAP = 0.15


@dataclass(frozen=True)
class Change:
    kind: str  # skip_category | raise_edge | rag_skip
    value: str
    reason: str


def _gap(summary: dict[str, Any]) -> float | None:
    model, market = summary.get("model_brier"), summary.get("market_brier")
    if not isinstance(model, float) or not isinstance(market, float):
        return None
    if math.isnan(model) or math.isnan(market):
        return None
    return model - market


def propose(rows: list[Any], s: Settings) -> list[Change]:
    """Changes the current report justifies. Empty when nothing is clear yet."""
    out: list[Change] = []
    lanes = report.by_lane(rows)
    have = {c.casefold() for c in s.skip_categories}
    for category, summary in report.by_category(rows).items():
        if (
            not category
            or category == "unknown"
            or category.casefold() in have
            or category.casefold() in KEEP_CATEGORIES
        ):
            continue
        if summary["settled"] < CATEGORY_MIN_SETTLED:
            continue
        gap = _gap(summary)
        if gap is not None and gap >= CATEGORY_BRIER_GAP:
            out.append(Change(
                "skip_category", category,
                f"{category}: {summary['settled']} settled, model Brier {summary['model_brier']:.3f} "
                f"vs market {summary['market_brier']:.3f}",
            ))

    moon = lanes.get("moonshot")
    if moon and moon["bets"] >= EDGE_MIN_BETS and moon["bet_return_pct"] < 0 and s.min_edge < EDGE_CAP:
        gap = _gap(moon)
        if gap is not None and gap >= LANE_BRIER_GAP:
            nxt = min(EDGE_CAP, round(s.min_edge + EDGE_STEP, 4))
            if nxt > s.min_edge:
                out.append(Change(
                    "raise_edge", f"{nxt:.2f}",
                    f"moonshot bets {moon['bet_return_pct']:.0f}% over {moon['bets']} bets; "
                    f"min edge {s.min_edge:.2f} -> {nxt:.2f}",
                ))
    return out


def apply(s: Settings, changes: list[Change]) -> list[str]:
    """Write the changes into data/tuning.json. Returns the log lines."""
    if not changes:
        return []
    data = read_tuning(s.data_dir)
    notes: list[str] = []
    for change in changes:
        if change.kind == "steady_off":
            data["steady_enabled"] = False
        elif change.kind == "skip_category":
            added = list(data.get("skip_categories_add") or [])
            if change.value not in added:
                added.append(change.value)
            data["skip_categories_add"] = added
        elif change.kind == "raise_edge":
            data["min_edge"] = float(change.value)
        elif change.kind == "rag_skip":
            lanes = list(data.get("rag_skip_lanes") or [])
            if change.value not in lanes:
                lanes.append(change.value)
            data["rag_skip_lanes"] = lanes
        else:
            continue
        notes.append(f"review: {change.reason}")
    log = list(data.get("log") or [])
    log.append({"ts": time.time(), "notes": notes})
    data["log"] = log[-30:]
    tuning_path(s.data_dir).write_text(json.dumps(data, indent=2) + "\n")
    return notes


def run_review(s: Settings, store) -> list[str]:
    """Score settled decisions, apply any clear tightening, rebuild the index if needed."""
    rows = store.settled_decisions()
    indexed = store.rebuild_rag()
    changes = propose(rows, s)
    notes = apply(s, changes)
    if any(c.kind == "rag_skip" for c in changes):
        indexed = store.rebuild_rag()
    if not notes:
        return [f"review: no change ({len(rows)} settled rows, {indexed} in the similar-case index)"]
    notes.append(f"similar-case index rebuilt ({indexed} rows)")
    return notes
