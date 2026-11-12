"""A library of curve features, each defined once (2026-11-12, user: "curves identify different features (dozens, maybe hundreds)"; "not even curve predictions, just curve features").

A feature is a named reading of the market's own curve at one moment, in units of that curve's own variance (the typical move of the window it reads: the one-second root-mean-square log move times sqrt(window seconds)), never in cents or by asset. A feature predicts nothing by itself; scripts/feature_library.py scores each one alone on what the contract's price does next.

`moment_features(g, sq, k, strike_log, sign, flow, quote)` returns {name: value} for one side at second index k of the one-second log-price grid g (sq = cumulative squared one-second moves, as curvemodel builds them). Families, each over the windows in SCALES (minutes):
  level_w      the cushion on the bet's side now, in the window's typical move
  slope_w      how far the cushion moved over the window, in the same unit
  curv_w       the cushion path's bend (parabola fit, curvemodel's) at 5, 13, 34, 180 minutes
  vol_w        the window's typical-move rate relative to the 55-minute rate (above 1: the curve is speeding up)
  range_w      the window's high minus low of the cushion
  eff_w        path efficiency: net move over the sum of the sampled absolute moves (1 = a straight line, 0 = round trip)
  fromhigh_w   how far the cushion is under its window high (what a sale gives back), fromlow_w how far over its window low
  cross_w      crossings of the strike inside the window (sampled every 15 s), and sincecross (minutes since the last one, 180 at most)
  bounce_w     the largest counter-move against the window's drift, in the window's unit
Plus the contract's own price and volume (what Kalshi itself is doing):
  mom_w        the bet side's bid now minus the bid w minutes ago (w in 1, 2, 3), in cents
  flowimb_s    taker volume on the bet's side minus the other side, over total, over the last s seconds (30, 60, 180)
  flowspike_s  the last s seconds' volume against the mean for the same length over the earlier ten minutes (above 1: a spike)
  spread, ask, left_min  the bet's spread, price and minutes left (reference columns; ask is the baseline every feature must add to)
"""

from __future__ import annotations

import itertools
import math
from typing import Any

from .curvemodel import _PARABOLA, QUAD_SAMPLES

SCALES = (1, 2, 3, 5, 8, 13, 21, 34, 55, 180)  # minutes
CURV_SCALES = (5, 13, 34, 180)
STEP = 15  # seconds between samples inside a window, for range, efficiency, crossings


def _unit(sq: list[float], k: int, look: int) -> float | None:
    if k - look < 0:
        return None
    rate2 = (sq[k] - sq[k - look]) / look
    return math.sqrt(rate2 * look) if rate2 > 0 else None


def _curv(g: list[float | None], sq: list[float], k: int, strike_log: float, sign: int, look: int) -> tuple[float, float, float] | None:
    unit = _unit(sq, k, look)
    if unit is None:
        return None
    ys: list[float] = []
    for i in range(QUAD_SAMPLES):
        x = g[k - look + round(i * look / (QUAD_SAMPLES - 1))]
        if x is None:
            return None
        ys.append((x - strike_log) * sign / unit)
    a, b, c = (sum(w[i] * ys[i] for i in range(QUAD_SAMPLES)) for w in _PARABOLA)
    return a, b, c


def moment_features(g: list[float | None], sq: list[float], k: int, strike_log: float, sign: int, flow: dict[str, float] | None = None, quote: dict[str, float] | None = None) -> dict[str, float]:
    """Every feature that can be read at this moment; a feature whose window is not covered (not enough history, a gap, a flat curve) is left out, not zeroed."""
    out: dict[str, float] = {}
    now_x = g[k]
    if now_x is None:
        return out
    rate55 = None
    u55 = _unit(sq, k, 55 * 60)
    if u55 is not None:
        rate55 = u55 / math.sqrt(55 * 60)
    for m in SCALES:
        look = 60 * m
        unit = _unit(sq, k, look)
        if unit is None:
            continue
        x0 = g[k - look]
        if x0 is None:
            continue
        cush_now = (now_x - strike_log) * sign / unit
        out[f"level_{m}m"] = cush_now
        out[f"slope_{m}m"] = (now_x - x0) * sign / unit
        if rate55:
            out[f"vol_{m}m"] = (unit / math.sqrt(look)) / rate55
        pts: list[float] = []
        for j in range(k - look, k + 1, max(STEP, look // 60)):
            x = g[j]
            if x is not None:
                pts.append((x - strike_log) * sign / unit)
        if len(pts) < 4:
            continue
        hi, lo = max(pts), min(pts)
        out[f"range_{m}m"] = hi - lo
        out[f"fromhigh_{m}m"] = hi - cush_now
        out[f"fromlow_{m}m"] = cush_now - lo
        moves = sum(abs(b - a) for a, b in itertools.pairwise(pts))
        out[f"eff_{m}m"] = abs(pts[-1] - pts[0]) / moves if moves > 0 else 0.0
        out[f"cross_{m}m"] = float(sum(1 for a, b in itertools.pairwise(pts) if (a < 0) != (b < 0)))
        drift = pts[-1] - pts[0]
        run, worst = pts[0], 0.0  # the largest counter-move against the drift's direction
        for p in pts[1:]:
            if drift >= 0:
                run = max(run, p)
                worst = max(worst, run - p)
            else:
                run = min(run, p)
                worst = max(worst, p - run)
        out[f"bounce_{m}m"] = worst
        if m == 180:  # minutes since the last crossing, read on the longest window
            last = None
            for idx in range(len(pts) - 1, 0, -1):
                if (pts[idx - 1] < 0) != (pts[idx] < 0):
                    last = len(pts) - 1 - idx
                    break
            out["sincecross"] = (last * max(STEP, look // 60) / 60.0) if last is not None else 180.0
    for m in CURV_SCALES:
        c = _curv(g, sq, k, strike_log, sign, 60 * m)
        if c is not None:
            out[f"curv_{m}m"] = c[2]
    if flow:
        out.update(flow)
    if quote:
        out.update(quote)
    return out


def feature_names(sample: dict[str, Any]) -> list[str]:
    return sorted(sample)
