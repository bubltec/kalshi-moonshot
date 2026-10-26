"""A forecast of a 15-minute crypto favorite's next few minutes from the settlement index's recent moves (no orders here).

The weights come from scripts/multiwindow_short_study.py (FC_MODE=freeze writes them to a JSON file): a least-squares fit of the market's next mid move on six
non-overlapping look-back slices of the index (0-3, 3-7, 7-15, 15-30, 30-60 and 60-120 minutes back), each signed toward the side and scaled by that coin's usual move over the slice.
The file is data, not code: refit it, or point FORECAST_WEIGHTS at another, and the lane follows. A review of frozen weights against what followed lives in the same script.
"""

from __future__ import annotations

import heapq
import json
import math
import os
from typing import Any

EDGES = (0, 3, 7, 15, 30, 60, 120)  # minutes back; slice i covers EDGES[i]..EDGES[i+1]
PRICE_MIN, PRICE_MAX = 0.0001, 0.9999  # Kalshi quotes 0.01c to 99.99c: a forecast price stays on that scale
MAX_AGE = 30.0  # an index tick older than this is not a reading


class Weights:
    def __init__(self, raw: dict[str, Any]):
        self.beta: list[float] = [float(x) for x in raw["beta"]]
        self.sigma: dict[str, dict[int, float]] = {ix: {int(w): float(v) for w, v in d.items()} for ix, d in raw["sigma"].items()}
        self.horizon: int = int(raw["horizon"])
        self.cutoff: float = float(raw.get("cutoff", 0.0))
        self.k: int = int(raw.get("k", 100))
        self.blend: float = float(raw.get("blend", 0.5))  # share of the forecast taken from similar past moments (0 = the regression alone)
        self.cases: list[list[float]] = [[float(v) for v in c] for c in raw.get("cases", [])]  # each: the six slice moves, then the move that followed
        if len(self.beta) != len(EDGES):
            raise ValueError(f"expected {len(EDGES)} weights (intercept + {len(EDGES) - 1} slices), got {len(self.beta)}")


_cache: dict[str, tuple[float, Weights]] = {}


def load_weights(path: str) -> Weights:
    """The weights in `path`, re-read when the file changes."""
    mtime = os.path.getmtime(path)
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    with open(path) as f:
        w = Weights(json.load(f))
    _cache[path] = (mtime, w)
    return w


def _value(tape: Any, index_id: str, at: float) -> float | None:
    row = tape.execute("SELECT value FROM index_ticks WHERE index_id=? AND ts<=? AND ts>? ORDER BY ts DESC LIMIT 1", (index_id, at, at - MAX_AGE)).fetchone()
    return float(row[0]) if row and row[0] else None


def slice_moves(tape: Any, weights: Weights, index_id: str, side: str, now: float) -> list[float] | None:
    """The six slice moves toward `side`, in each slice's usual size. None when the index has no reading for one of the points or no scale for the coin."""
    scale = weights.sigma.get(index_id)
    if scale is None:
        return None
    points = [_value(tape, index_id, now - m * 60.0) for m in EDGES]
    if any(p is None for p in points):
        return None
    sign = 1.0 if side == "yes" else -1.0
    out = []
    for i in range(len(EDGES) - 1):
        lo, hi = EDGES[i], EDGES[i + 1]
        usual = scale.get(hi)
        if not usual:
            return None
        out.append(sign * math.log(points[i] / points[i + 1]) / (usual * math.sqrt((hi - lo) / hi)))  # type: ignore[operator]
    return out


def predict(weights: Weights, moves: list[float]) -> float:
    """The expected change of the side's mid over the horizon, in dollars (0.01 = one cent)."""
    return weights.beta[0] + sum(b * z for b, z in zip(weights.beta[1:], moves, strict=True))


def recalled(weights: Weights, moves: list[float]) -> float | None:
    """The average move that followed the `k` past moments whose six slice moves are closest to these. None when there is no memory."""
    if not weights.cases:
        return None
    near = heapq.nsmallest(weights.k, weights.cases, key=lambda c: sum((a - b) ** 2 for a, b in zip(c, moves, strict=False)))
    return sum(c[-1] for c in near) / len(near)


def forecast_move(weights: Weights, moves: list[float]) -> tuple[float, float, float | None]:
    """(blended move, regression move, recalled move). The blend is the regression alone when the file carries no cases."""
    reg = predict(weights, moves)
    memory = recalled(weights, moves)
    if memory is None:
        return reg, reg, None
    return (1.0 - weights.blend) * reg + weights.blend * memory, reg, memory


def load_gates(path: str) -> dict[str, dict[str, Any]]:
    """The skip rules in `path` (behavior.write_gates), re-read when the file changes. Empty when there is no file."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    hit = _gate_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    with open(path) as f:
        gates: dict[str, dict[str, Any]] = json.load(f).get("gates", {})
    _gate_cache[path] = (mtime, gates)
    return gates


_gate_cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}


def gates_tripped(gates: dict[str, dict[str, Any]], values: dict[str, float | None]) -> list[str]:
    """The skip rules these values fall under: a feature in the worst quarter of its recorded range (the end the record says does worse), with the cutoff and the end both read from the file."""
    out = []
    for name, g in gates.items():
        v = values.get(name)
        if v is None:
            continue
        if (g["worse"] == "high" and v >= g["cutoff"]) or (g["worse"] == "low" and v <= g["cutoff"]):
            out.append(f"{name} {v:+.3g} is in the worse quarter ({g['worse']} end, cutoff {g['cutoff']:+.3g})")
    return out
