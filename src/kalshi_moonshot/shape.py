"""Shapes of 15-minute markets: how a contract's price moved through its window, and how similar past windows ended.

A window's shape is the YES price (mid of bid and ask) at the end of each of its 15 minutes. The estimator for the `llm_shape` lane shows a
language model the current window's shape so far, the closest past windows of the same instrument with how each one settled, and the plain
empirical rate for windows that stood at a similar price at the same minute. This module is the data part: building shapes, finding
neighbours and writing the prompt. It places nothing and calls no model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

WINDOW_MINUTES = 15
WINDOW_SECONDS = WINDOW_MINUTES * 60

Path = list[float | None]  # index 0 is the end of minute 1 ... index 14 the end of minute 15


@dataclass(frozen=True)
class Window:
    """A settled window: its shape and how it ended."""

    ticker: str
    close_ts: float
    result: str  # "yes" or "no"
    path: Path


@dataclass(frozen=True)
class Neighbor:
    window: Window
    distance: float


def _mid(bid: Any, ask: Any) -> float | None:
    try:
        b, a = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    if a <= 0 or b < 0 or a < b:
        return None
    return (a + b) / 2.0


def path_from_candles(rows: list[tuple[float, Any, Any]], close_ts: float) -> Path:
    """Minute-end bid/ask rows -> the 15 minute-end mids. A missing minute stays None."""
    path: Path = [None] * WINDOW_MINUTES
    open_ts = close_ts - WINDOW_SECONDS
    for end_ts, bid, ask in rows:
        i = round((float(end_ts) - open_ts) / 60.0) - 1
        if 0 <= i < WINDOW_MINUTES:
            path[i] = _mid(bid, ask)
    return path


def path_from_quotes(quotes: list[tuple[float, Any, Any]], open_ts: float, minutes: int) -> Path:
    """Quote ticks (time, bid, ask), oldest first -> the mids at the end of minutes 1..minutes: the last quote at or before each minute mark."""
    out: Path = []
    j = -1
    for i in range(1, minutes + 1):
        mark = open_ts + 60.0 * i
        while j + 1 < len(quotes) and quotes[j + 1][0] <= mark:
            j += 1
        out.append(_mid(quotes[j][1], quotes[j][2]) if j >= 0 else None)
    return out


def distance(current: Path, past: Path, minutes: int) -> float | None:
    """How far two shapes are apart over the first `minutes` minutes: root-mean-square difference of the mids, later minutes counting more.
    None when the past window is missing too much of that stretch to compare."""
    num = den = 0.0
    seen = 0
    for i in range(min(minutes, len(current))):
        a, b = current[i], past[i] if i < len(past) else None
        if a is None or b is None:
            continue
        w = (i + 1) / minutes
        num += w * (a - b) ** 2
        den += w
        seen += 1
    if seen < max(2, minutes - 2):
        return None
    return math.sqrt(num / den)


def nearest(current: Path, pool: list[Window], minutes: int, k: int = 5) -> list[Neighbor]:
    scored = []
    for w in pool:
        d = distance(current, w.path, minutes)
        if d is not None:
            scored.append(Neighbor(w, d))
    scored.sort(key=lambda n: n.distance)
    return scored[:k]


def rate_near(pool: list[Window], minute: int, mid: float, band: float) -> tuple[int, float | None]:
    """Windows whose mid at `minute` was within `band` of `mid`: how many, and the share that settled YES."""
    hits = 0
    yes = 0
    for w in pool:
        px = w.path[minute - 1] if minute - 1 < len(w.path) else None
        if px is not None and abs(px - mid) <= band:
            hits += 1
            yes += w.result == "yes"
    return (hits, yes / hits) if hits else (0, None)


def _cents(v: float | None) -> str:
    return "-" if v is None else str(round(v * 100))


def _fmt(path: Path, upto: int | None = None) -> str:
    """Prices in whole cents: `48 58 55` costs a quarter of the tokens `0.48 0.58 0.55` does."""
    return " ".join(_cents(v) for v in (path if upto is None else path[:upto]))


def _later(done: int) -> list[int]:
    """The minutes after `done` whose price tells how a window went on: a few checkpoints, then the last minute."""
    marks = [m for m in (done + 2, done + 5, done + 8) if m < WINDOW_MINUTES]
    return [*marks, WINDOW_MINUTES]


HEAD = """You estimate the chance that a short-term market settles YES, using only the shapes of past markets like it.

Market: {title}
15-minute window; YES pays $1 if the instrument finishes above where it started, else $0. It is minute {minute} of 15.
YES price now (cents): bid {bid}, ask {ask}.

This window so far: YES price in cents at the end of minutes 1 to {done}:
{current}
"""

NEIGHBORS = """
The {n} closest past windows of the same instrument. Each line: its prices (cents) at the end of minutes 1 to {done}, then | its prices at minutes {marks} | how it settled.
{lines}
"""

RATES = """
Plain rates from {total} settled past windows of this instrument, YES share:
- all windows: {all_rate}
- price at minute {done} within 5 cents of now ({n5} windows): {r5}
- price at minute {done} within 10 cents of now ({n10} windows): {r10}
"""

TAIL = """
Past windows are your only evidence. If the closest ones disagree or are few, stay near the plain rates. Do not assume the price is right or wrong.
Reply with ONLY JSON: {{"probability": <chance YES, 0.001 to 0.999>, "rationale": "<at most 12 words>"}}"""


def _pct(rate: float | None) -> str:
    return "none" if rate is None else f"{rate:.0%}"


def build_prompt(title: str, minute: int, bid: float, ask: float, current: Path, neighbors: list[Neighbor], pool: list[Window]) -> str:
    done = max(1, minute - 1)
    parts = [HEAD.format(title=title, minute=minute, bid=round(bid * 100), ask=round(ask * 100), done=done, current=_fmt(current, done))]
    if neighbors:
        later = _later(done)
        lines = "\n".join(
            f"{_fmt(n.window.path, done)} | {' '.join(_cents(n.window.path[m - 1]) for m in later)} | {n.window.result.upper()}"
            for n in neighbors
        )
        parts.append(NEIGHBORS.format(n=len(neighbors), done=done, marks=", ".join(str(m) for m in later), lines=lines))
    last = current[done - 1] if len(current) >= done else None
    now_px: float = last if last is not None else (bid + ask) / 2.0
    n5, r5 = rate_near(pool, done, now_px, 0.05)
    n10, r10 = rate_near(pool, done, now_px, 0.10)
    all_rate = sum(w.result == "yes" for w in pool) / len(pool) if pool else None
    parts.append(RATES.format(total=len(pool), all_rate=_pct(all_rate), done=done, n5=n5, r5=_pct(r5), n10=n10, r10=_pct(r10)))
    parts.append(TAIL.format())
    return "".join(parts)
