"""Sweep lane settings over tapes: the only way a study varies a rule.

The rules live in the watcher, once. A study changes a lane setting (shock_bp, wrong_side_seconds, ...) and replays the tapes through the same
code the bot runs; a new idea is first built into the watcher as a lane setting that is off by default, then swept here. No study re-implements
a rule, so a result here is a result for the live bot.
"""

from __future__ import annotations

import itertools
import tomllib
from collections.abc import Iterable, Iterator
from typing import Any

from .harness import Outcome, run
from .tape import Tape


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k} = {_value(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list | tuple):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    return repr(v)


def override(lanes_toml: str, lane: str, **settings: Any) -> str:
    """The lane file with `settings` set on `lane` (flat tables only, which is all a strategies.toml uses)."""
    doc = tomllib.loads(lanes_toml)
    doc["lane"][lane].update(settings)
    out: list[str] = []
    for name, tbl in doc["lane"].items():
        out.append(f"[lane.{name}]")
        out += [f"{k} = {_value(v)}" for k, v in tbl.items()]
        out.append("")
    return "\n".join(out)


def grid_points(grid: dict[str, list[Any]]) -> Iterator[dict[str, Any]]:
    keys = list(grid)
    for combo in itertools.product(*(grid[k] for k in keys)):
        yield dict(zip(keys, combo, strict=True))


def sweep(tapes: Iterable[Tape], lanes_toml: str, grid: dict[str, list[Any]], lane: str | None = None, fixed: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """One row per (tape, grid point): the settings, and what the watcher did. `lane` defaults to each tape's own lane."""
    tapes = list(tapes)
    rows: list[dict[str, Any]] = []
    for point in grid_points(grid):
        for t in tapes:
            o: Outcome = run(t, override(lanes_toml, lane or t.lane, **{**(fixed or {}), **point}))
            rows.append({**point, "tape": o.tape, "series": t.ticker.split("-")[0], "variety": t.variety, "signals": o.signals, "sold": o.sold, "stuck": o.stuck,
                         "exit_left": o.exit_left, "sold_pnl": o.sold_pnl, "hold_pnl": o.hold_pnl, "delta": o.delta, "loser": t.hold_pnl() < 0})
    return rows


def summarize(rows: list[dict[str, Any]], by: list[str]):
    """Per grid point: bets, sales, sales on bets that would have lost, and the total change against holding."""
    import pandas as pd  # type: ignore[import-untyped]

    df = pd.DataFrame(rows)
    df["sale_on_loser"] = df["sold"] & df["loser"]
    g = df.groupby(by, dropna=False)
    return g.agg(bets=("tape", "count"), sales=("sold", "sum"), on_losers=("sale_on_loser", "sum"), stuck=("stuck", "sum"), delta=("delta", "sum"), hold=("hold_pnl", "sum")).round(2)
