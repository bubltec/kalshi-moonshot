"""A study is a YAML file: the lane, how far back to take its held bets, and a grid of lane settings (see studies/)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]


@dataclass
class Study:
    name: str
    description: str
    lane: str
    since_days: float
    grid: dict[str, list[Any]]


def load(path: Path) -> Study:
    d = yaml.safe_load(path.read_text())
    return Study(path.stem, str(d.get("description", "")), d["lane"], float(d.get("since_days", 14)), {k: list(v) for k, v in d["grid"].items()})


STATUSES = ("promising", "needs_work", "untested", "dead")


def concepts(path: Path) -> list[dict[str, Any]]:
    """The strategy board, each entry checked for a known status."""
    rows = yaml.safe_load(path.read_text())["concepts"]
    for c in rows:
        if c["status"] not in STATUSES:
            raise ValueError(f"{c['id']}: status {c['status']!r} is not one of {STATUSES}")
    return list(rows)
