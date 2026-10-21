"""Lanes as data. A lane is a set of parameters plus the name of the estimator that rates it.

Nothing about a lane is written into the code paths: the filter, the trend rule, the bet rule and
the order size all read a LaneSpec. The defaults come from the same environment settings as
before. A `strategies.toml` in DATA_DIR can change a lane's fields or add a whole new lane:

    [lane.metals_long]
    estimator = "ladder"
    price = [0.02, 0.30]
    hours = [1, 48]
    min_edge = 0.10
    series = ["KXGOLDD", "KXSILVERD"]

The file is read by lane_specs(), so editing it applies on the next scan.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import Settings

LLM = "llm"
LADDER = "ladder"


@dataclass(frozen=True)
class LaneSpec:
    name: str
    estimator: str  # key in the estimator registry (estimators.py)
    label: str  # used in reject messages, e.g. "ask outside {label} range"
    price: tuple[float, float]  # ask range, dollars
    hours: tuple[float, float]  # hours to close range
    max_spread: float
    min_volume: float
    min_edge: float | None = None  # bet when the calibrated edge reaches this ...
    max_lag: float | None = None  # ... or, when set, when the estimate is no more than this under the ask
    usd_per_trade: float | None = None  # optional dollar ceiling per bet; None/0 = sized by balance
    fixed_stake: float = 0.0  # dollars per bet, ignoring Kelly (0 = size by the odds and the balance)
    always: bool = False  # bet whenever the filters pass, with no minimum edge from a model
    min_age_minutes: float = 0.0  # skip a market until it has been open this long (books settle down)
    min_chance: float = 0.0  # bet only when the estimator's chance that the contract pays is in [min_chance, max_chance]
    max_chance: float = 1.0  # (the estimate before calibration; for a ladder lane this is a distance to the strike)
    take_profit_x: float = 0.0  # sell when the bid reaches this multiple of the price paid (0 = off); watched live on the socket
    take_profit_at: float = 0.0  # sell when the bid reaches this price (0 = off)
    trail_activate: float = 0.0  # peak-crash exit: once the bid has been this fraction above the price paid (0.10 = up 10%) ...
    trail_drop: float = 0.0  # ... sell when it falls this many dollars from its peak (0 = off; watched live on the socket only)
    spike_cents: float = 0.0  # sell, while in profit, when the bid has risen this much (dollars) within spike_seconds (0 = off)
    spike_seconds: float = 30.0
    min_return: float = 0.0  # a win must pay at least this much on the stake, after the fee
    max_bet_pct: float = 0.02  # one bet, share of equity (see sizing.py)
    max_exposure_pct: float = 0.25  # all open bets in the lane, share of equity
    shortlist: int = 10
    numeric: str = "skip"  # "skip" | "allow" markets that resolve on a live number
    trend: str = "none"  # "none" | "rising" (needs history) | "not_falling" (no history is fine)
    one_per: str = "none"  # "none" | "mutex" (one side per exclusive event) | "event" (best edge only)
    series: tuple[str, ...] = ()  # restrict to these series; empty = any
    needs_instrument: bool = False  # series must be a priced commodity (instruments.toml)
    lookback_hours: int = 6  # candle window for the trend rule
    min_trend: float = 0.0  # ask gain over the window a 'rising' lane needs, dollars
    max_drop: float = 0.02  # ask fall (window or last candle) a 'not_falling' lane tolerates
    scope_minutes: float | None = None  # with review_minutes: slow pass that builds the watchlist
    review_minutes: float | None = None  # fast pass over the watchlist; None = the main scan only
    cooldown_minutes: float = 0.0  # skip a market decided on this recently
    watch_max: int = 40
    priority: int = 0  # queue priority of this lane's jobs; higher runs first
    trust: float = 1.0  # prior weight on the estimator before our record has any say (0 to 1)
    sides: tuple[str, ...] = ("yes",)  # contracts the lane may buy: "yes", "no" or both
    timed_suffix: str = ""  # series ending in this are fixed-length windows with an entry window
    timed_minutes: float = 15.0  # length of such a window
    entry_elapsed: tuple[float, float] = (0.0, 1e9)  # bet only this many minutes after it opens
    shadow_suffix: str = ""  # series ending in this are only measured (see shadow.py), never bet
    enabled: bool = True


def _pair(v: Any) -> tuple[float, float]:
    lo, hi = v
    return float(lo), float(hi)


def builtin_lanes(s: Settings) -> dict[str, LaneSpec]:
    """The three starting lanes, parameterized entirely from settings."""
    lanes = {
        "moonshot": LaneSpec(
            "moonshot", LLM, "long-shot", (s.min_price, s.max_price),
            (s.min_hours_to_close, s.max_hours_to_close), s.max_spread, s.min_volume,
            min_edge=s.min_edge, shortlist=s.shortlist, trend="not_falling", one_per="mutex",
            max_bet_pct=s.moonshot_bet_pct, max_exposure_pct=s.moonshot_exposure_pct,
            lookback_hours=s.steady_lookback_hours, max_drop=s.moonshot_max_drop,
            sides=s.moonshot_sides,
        ),
        "steady": LaneSpec(
            "steady", LLM, "steady", (s.steady_min_price, s.steady_max_price),
            (s.steady_min_hours_to_close, s.steady_max_hours_to_close), s.steady_max_spread,
            s.steady_min_volume, max_lag=s.steady_max_lag, usd_per_trade=s.steady_usd_per_trade or None,
            shortlist=s.steady_shortlist, trend="rising", one_per="mutex", enabled=s.steady_enabled,
            max_bet_pct=s.steady_bet_pct, max_exposure_pct=s.steady_exposure_pct,
            lookback_hours=s.steady_lookback_hours, min_trend=s.steady_min_trend,
            sides=s.steady_sides,
        ),
        "commodity": LaneSpec(
            "commodity", LADDER, "commodity", (s.commodity_min_price, s.commodity_max_price),
            (s.commodity_min_hours_to_close, s.commodity_max_hours_to_close),
            s.commodity_max_spread, s.commodity_min_volume, min_edge=s.commodity_min_edge,
            usd_per_trade=s.commodity_usd_per_trade or None, shortlist=s.commodity_shortlist,
            numeric="allow", one_per="event", needs_instrument=True, enabled=s.commodities_enabled,
            sides=("yes", "no"),
            scope_minutes=s.commodity_scope_minutes, review_minutes=s.commodity_review_minutes,
            cooldown_minutes=s.commodity_cooldown_minutes, watch_max=s.commodity_watch_max,
            priority=10, min_return=s.commodity_min_return, min_age_minutes=s.commodity_min_age_minutes, shadow_suffix="15M", timed_suffix="15M",
            entry_elapsed=(s.commodity_fast_from_min, s.commodity_fast_to_min),
            max_bet_pct=s.commodity_bet_pct, max_exposure_pct=s.commodity_exposure_pct,
        ),
    }
    return lanes


def _overlay(base: LaneSpec | None, name: str, row: dict[str, Any]) -> LaneSpec:
    known = {f.name for f in fields(LaneSpec)}
    unknown = set(row) - known
    if unknown:
        raise ValueError(f"lane {name!r}: unknown field(s) {sorted(unknown)}")
    row = dict(row)
    for key in ("price", "hours", "entry_elapsed"):
        if key in row:
            row[key] = _pair(row[key])
    for key in ("series", "sides"):
        if key in row:
            row[key] = tuple(row[key])
    if bad := set(row.get("sides", ())) - {"yes", "no"}:
        raise ValueError(f"lane {name!r}: sides must be \"yes\" and/or \"no\", got {sorted(bad)}")
    if base is None:
        missing = {"estimator", "price", "hours"} - set(row)
        if missing:
            raise ValueError(f"lane {name!r}: new lanes need {sorted(missing)}")
        row.setdefault("label", name)
        row.setdefault("max_spread", 0.05)
        row.setdefault("min_volume", 0.0)
        return LaneSpec(name=name, **row)
    return replace(base, **row)


def lane_specs_from(s: Settings, strategies_toml: str | None) -> dict[str, LaneSpec]:
    lanes = builtin_lanes(s)
    if strategies_toml:
        for name, row in (tomllib.loads(strategies_toml).get("lane") or {}).items():
            lanes[name] = _overlay(lanes.get(name), name, row)
    for spec in lanes.values():
        if tiered(spec) and not (spec.series or spec.needs_instrument):
            raise ValueError(
                f"lane {spec.name!r}: review_minutes needs `series` or `needs_instrument`, "
                "because a fast pass can only fetch what it can name"
            )
    return lanes


@lru_cache(maxsize=8)
def _cached(s: Settings, text: str | None) -> dict[str, LaneSpec]:
    return lane_specs_from(s, text)


def strategies_path(data_dir: Path) -> Path:
    return data_dir / "strategies.toml"


def lane_specs(s: Settings) -> dict[str, LaneSpec]:
    """Lanes for these settings, with DATA_DIR/strategies.toml applied. Cached per file content."""
    path = strategies_path(s.data_dir)
    text = path.read_text() if path.is_file() else None
    return _cached(s, text)


def enabled_lanes(s: Settings) -> list[LaneSpec]:
    return [lane for lane in lane_specs(s).values() if lane.enabled]


def unknown_live_lanes(s: Settings) -> list[str]:
    """Names in LIVE_LANES that are not lanes. A typo there silently trades nothing, so it is reported."""
    return sorted((s.live_lanes or frozenset()) - set(lane_specs(s)))


def tiered(spec: LaneSpec) -> bool:
    """True for a two-level lane: scoped slowly into a watchlist, reviewed quickly from it."""
    return spec.review_minutes is not None


def main_lanes(s: Settings) -> list[LaneSpec]:
    """Enabled lanes served by the all-markets scan."""
    return [lane for lane in enabled_lanes(s) if not tiered(lane)]


def tiered_lanes(s: Settings) -> list[LaneSpec]:
    return [lane for lane in enabled_lanes(s) if tiered(lane)]


def spec_for(s: Settings, name: str) -> LaneSpec:
    lanes = lane_specs(s)
    if name not in lanes:
        raise KeyError(f"unknown lane {name!r} (known: {', '.join(sorted(lanes))})")
    return lanes[name]


def takes_profit(spec: LaneSpec) -> bool:
    return spec.take_profit_x > 0 or spec.take_profit_at > 0 or spec.trail_drop > 0 or spec.spike_cents > 0


def chance_ok(spec: LaneSpec, p: float) -> bool:
    return spec.min_chance - 1e-9 <= p <= spec.max_chance + 1e-9


def wants_bet(spec: LaneSpec, p: float, ask: float, edge: float) -> bool:
    """The lane's bet rule, from its parameters. An edge bar and a lag bar can both be set."""
    if spec.always:
        return True
    if spec.max_lag is not None and ask - p > spec.max_lag + 1e-9:
        return False
    if spec.min_edge is not None and edge < spec.min_edge:
        return False
    return spec.max_lag is not None or spec.min_edge is not None
