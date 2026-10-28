"""Hourly review of the scorecard and the settled-case index.

It only tightens settings, and only when a slice of the report is large enough to be clear.
A weak lane stays on: a few bad settled cases are not a reason to drop the whole book.
It cannot turn trading on or raise a risk cap. Undo a change by deleting that key from
data/tuning.json (or the whole file).
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import digest, report
from .config import Settings, read_tuning, tuning_path

log = logging.getLogger("moonshot")

SUGGEST_HEAD = """You are reviewing one hour of the bot's 15-minute ladder favorites (crypto, FX, metals/energy). These markets turn over every 15 minutes, so the first repeating grain is the hour of the New York day, then the weekday, then the week. Compare THIS hour to the same clock hour over the last 7 days, and to the 24-hour profile. If saved weekday-hour slices exist, say whether this Monday-at-12 (etc.) is repeating.

Suggest improvements the operator can test on 15-minute markets. Do not change settings yourself. Do not invent numbers that are not in the report. Do not talk about hourly or daily markets unless the report does.

The book is already: resting buy, hold to settlement, a 45% stop whose wait is calculated from time left, and a volume-burst exit in the last 120s on any 15-minute market that actually trades (a silent book cannot 5x itself). Every lane except the moonshot lanes watches a lane line (strategies.toml: watch_at, watch_patient_left, watch_ride_seconds, watch_loss, watch_loss_seconds). A downtrend under watch_at is abandoned after the ride, which scales with time left; a hover is held; watch_loss is the hard fraction of the price paid, then watch_loss_seconds to bounce. Suggest changing those fields when this hour's stops say so, and name the field. early_scalp buys at 39c or under in minutes 1-7 (the price band; near 50c is passed) and, once the bid is scalp_up above the price paid, sells once the bid has stayed scalp_dip under the high since for scalp_dip_seconds (a climb rides, and a new high starts the count again). scalp_at is an absolute arm and is off on that lane. From cash_up_minute it also sells a bid above the price paid, or cash_drop under it, and with cash_left_seconds left it sells a hover still under the price paid. crypto_moonshot buys a 2-10c crypto side that has been sitting there while that side keeps getting bought, without holding the favorite. manual is your app fills, logged for study: cheap (≤25c) holds to settlement. Take-profit, trail, stop-model and settlement-guard are off because they cut winners. Do not suggest turning those back on unless this hour's numbers clearly reverse that.

Reply with ONLY JSON:
{"observations": ["one factual reading of this clock hour vs its 7-day baseline", "..."],
 "suggestions": [{"change": "a concrete, testable change to 15-minute lanes", "why": "which number in the report justifies it", "test": "how to tell at the next same clock hour if it helped"}]}
At most three suggestions. If nothing is clear, return an empty suggestions list and say so in observations.
"""

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


def _parse_suggestions(text: str) -> dict[str, Any]:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        raise ValueError("no JSON in model reply")
    data = json.loads(m.group(0))
    obs = [str(x)[:400] for x in (data.get("observations") or []) if str(x).strip()][:6]
    sugg = []
    for item in (data.get("suggestions") or [])[:3]:
        if not isinstance(item, dict):
            continue
        change, why, test = str(item.get("change") or "").strip(), str(item.get("why") or "").strip(), str(item.get("test") or "").strip()
        if change:
            sugg.append({"change": change[:400], "why": why[:400], "test": test[:400]})
    return {"observations": obs, "suggestions": sugg}


def hour_suggest(s: Settings, store, hours: float = 1.0) -> list[str]:
    """Send the last hour of bets to the local model. Writes suggestions to DATA_DIR/review-notes.md. Never applies them."""
    if not s.review_llm:
        return []
    from .llm import Ollama

    model = Ollama(s.ollama_url, s.model)
    if not model.ping():
        return ["review llm: ollama not reachable, skipped"]
    context = digest.hour_context(s, store, hours)
    prior = digest.notes(s)
    prompt = SUGGEST_HEAD + "\nThis clock hour:\n" + context[:9000]
    if prior and not prior[0].startswith("(no "):
        prompt += "\n\nOpen items from earlier reviews (do not repeat these unless this hour adds evidence):\n" + "\n".join(prior[-20:])
    try:
        raw = model.complete(prompt, tag="hour-review")
        got = _parse_suggestions(raw)
    except Exception as exc:
        log.warning("review llm: %s", exc)
        return [f"review llm: failed ({exc})"]
    when = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    block = [f"## {when} — last {hours:g}h"]
    for line in got["observations"]:
        block.append(f"- {line}")
    if got["suggestions"]:
        block.append("suggestions (not applied):")
        for i, item in enumerate(got["suggestions"], 1):
            block.append(f"{i}. {item['change']}")
            if item["why"]:
                block.append(f"   why: {item['why']}")
            if item["test"]:
                block.append(f"   test: {item['test']}")
    else:
        block.append("- no suggestions this hour")
    path = s.data_dir / "review-notes.md"
    existing = path.read_text() if path.is_file() else ""
    path.write_text(existing.rstrip() + ("\n\n" if existing.strip() else "") + "\n".join(block) + "\n")
    n = len(got["suggestions"])
    return [f"review llm: {n} suggestion(s) written to review-notes.md"]


def run_review(s: Settings, store) -> list[str]:
    """Score settled decisions, apply any clear tightening, rebuild the index if needed, then ask the model about the last hour."""
    rows = store.settled_decisions()
    indexed = store.rebuild_rag()
    changes = propose(rows, s)
    notes = apply(s, changes)
    if any(c.kind == "rag_skip" for c in changes):
        indexed = store.rebuild_rag()
    if not notes:
        notes = [f"review: no change ({len(rows)} settled rows, {indexed} in the similar-case index)"]
    else:
        notes.append(f"similar-case index rebuilt ({indexed} rows)")
    notes.extend(hour_suggest(s, store))
    return notes
