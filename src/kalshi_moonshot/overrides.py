"""Lane settings changed from the admin page, within limits that live here and nowhere else.

The admin page writes a document to S3 (control/overrides.json); control.py pulls it, checks it with validate() and, when it passes, writes DATA_DIR/overrides.json. lanes.lane_specs()
applies that file on top of strategies.toml, so a change takes effect on the next scan with no restart. The page and its login Lambda check too, but this is the check that counts:
a field not in TUNABLES, a value outside its bounds, or a lane that is not a lane is refused whatever the page sent. Lanes cannot be added, removed or made live from here, and the
fields that point a lane at other markets, estimators or risk gates are not in the list.

Document: {"revision": 7, "updated": "...", "by": "194750", "lanes": {"commodity": {"min_edge": 0.12, "enabled": true}}}
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

log = logging.getLogger("moonshot")

FILE = "overrides.json"


@dataclass(frozen=True)
class Tunable:
    kind: str  # "float" | "int" | "bool"
    low: float = 0.0  # inclusive bounds; for a bool they are unused
    high: float = 0.0
    label: str = ""
    unit: str = ""  # what the number is, for the page: "$", "%", "s", ""
    risk: str = "neutral"  # "up" when a larger value risks more money, "down" when a larger value risks less; the page warns on a move that adds risk


TUNABLES: dict[str, Tunable] = {
    "enabled": Tunable("bool", label="Lane on", risk="up"),
    "min_edge": Tunable("float", 0.0, 0.5, "Minimum edge to bet", "", "down"),
    "max_lag": Tunable("float", 0.0, 0.5, "Most the estimate may sit under the ask", "$", "up"),
    "min_chance": Tunable("float", 0.0, 1.0, "Lowest estimated chance to bet", "", "down"),
    "max_chance": Tunable("float", 0.0, 1.0, "Highest estimated chance to bet", "", "up"),
    "usd_per_trade": Tunable("float", 0.0, 50.0, "Dollar ceiling per bet (0 = sized by balance)", "$", "up"),
    "fixed_stake": Tunable("float", 0.0, 25.0, "Fixed dollars per bet (0 = sized by odds)", "$", "up"),
    "max_bet_pct": Tunable("float", 0.0, 0.05, "One bet, share of equity", "", "up"),
    "max_exposure_pct": Tunable("float", 0.0, 0.40, "All open bets, share of equity", "", "up"),
    "max_open": Tunable("int", 0, 20, "Most bets open at once (0 = no limit)", "", "up"),
    "min_dwell_seconds": Tunable("float", 0.0, 300.0, "Seconds a price must sit in the band before buying", "s", "down"),
    "reentry_cooldown_seconds": Tunable("float", 0.0, 3600.0, "Wait before buying a sold market again", "s", "down"),
    "buy_slip": Tunable("float", 0.0, 0.05, "Dollars over the ask a buy may pay", "$", "up"),
    "stop_loss_at": Tunable("float", 0.0, 0.9, "Stop, fraction under the price paid (0 = global)", "", "neutral"),
    "take_profit_x": Tunable("float", 0.0, 10.0, "Sell at this multiple of the price paid (0 = off)", "x", "neutral"),
    "take_profit_at": Tunable("float", 0.0, 1.0, "Sell when the bid reaches this price (0 = off)", "$", "neutral"),
    "watch_loss": Tunable("float", 0.0, 0.95, "Hard stop, fraction of the price paid lost (0 = off)", "", "neutral"),
    "cash_out_at": Tunable("float", 0.0, 1.0, "Sell when the bid falls to this price (0 = off)", "$", "neutral"),
    "drop_cents": Tunable("float", 0.0, 0.5, "Sell a favorite whose bid falls this many dollars (0 = off)", "$", "neutral"),
    "burst_dollars": Tunable("float", 0.0, 5000.0, "Burst exit bar, dollars bought against in 3 s (0 = off)", "$", "neutral"),
    "burst_buy": Tunable("float", 0.0, 10.0, "Dollars hedged on the other side at a burst", "$", "up"),
    "strike_through_sale": Tunable("float", 0.0, 1.0, "Strike-through sale, percent of the strike (0 = off)", "%", "neutral"),
    "scalp_dip": Tunable("float", 0.0, 0.2, "Scalp exit, dollars under the high", "$", "neutral"),
}

def check_value(field: str, value: Any) -> str | None:
    """The reason a value is refused, or None when it is allowed."""
    t = TUNABLES.get(field)
    if t is None:
        return f"{field} is not a tunable field"
    if t.kind == "bool":
        return None if isinstance(value, bool) else f"{field} must be true or false"
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return f"{field} must be a number"
    if t.kind == "int" and value != int(value):
        return f"{field} must be a whole number"
    if not t.low <= value <= t.high:
        return f"{field} must be between {t.low:g} and {t.high:g}"
    return None


def validate(doc: Any, lane_names: set[str]) -> list[str]:
    """Every reason the document is refused (empty = fine). `lane_names` are the lanes that exist."""
    if not isinstance(doc, dict) or not isinstance(doc.get("lanes"), dict):
        return ["the document needs a lanes object"]
    if not isinstance(doc.get("revision"), int) or isinstance(doc.get("revision"), bool) or doc["revision"] < 0:
        return ["the document needs a whole-number revision"]
    errors: list[str] = []
    for lane, changes in doc["lanes"].items():
        if lane not in lane_names:
            errors.append(f"{lane} is not a lane")
            continue
        if not isinstance(changes, dict):
            errors.append(f"{lane}: changes must be an object")
            continue
        errors.extend(f"{lane}: {e}" for field, value in changes.items() if (e := check_value(field, value)))
    return errors


def apply(specs: dict[str, Any], doc: Any) -> dict[str, Any]:
    """The lanes with the document's changes on top. Anything that fails the checks is skipped, never raised: a bad file must not stop the trading loop."""
    if not isinstance(doc, dict) or not isinstance(doc.get("lanes"), dict):
        return specs
    out = dict(specs)
    for lane, changes in doc["lanes"].items():
        if lane not in out or not isinstance(changes, dict):
            continue
        good = {k: (float(v) if TUNABLES[k].kind == "float" else v) for k, v in changes.items() if check_value(k, v) is None}
        if len(good) != len(changes):
            log.warning("overrides: skipped %s for lane %s", sorted(set(changes) - set(good)), lane)
        if good:
            out[lane] = replace(out[lane], **good)
    return out


def load_text(data_dir: Path) -> str | None:
    path = data_dir / FILE
    return path.read_text() if path.is_file() else None


def parse(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        log.warning("overrides: the file is not JSON; ignored")
        return None
    return doc if isinstance(doc, dict) else None


def schema() -> dict[str, dict[str, Any]]:
    """For the admin page: each field's kind, bounds and label."""
    return {name: {"kind": t.kind, "low": t.low, "high": t.high, "label": t.label, "unit": t.unit, "risk": t.risk} for name, t in TUNABLES.items()}


def effective(specs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each lane's current value of every tunable field, as the bot will use it."""
    return {name: {f: getattr(spec, f) for f in TUNABLES} for name, spec in specs.items()}
