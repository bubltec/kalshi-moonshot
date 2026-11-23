"""The market's scenario: where the target sits in the spot's recent scopes, how the last four markets went, and the cushion now.

2026-11-16, user: "our goal is to get rid of the price and time lapsed properties in favor of a smarter process"; "maybe we could build a structured buy scenario every 15 mins ... based on the previous hour of ... markets"; "one thing i do visually is look at the distance the target is from the spot (cushion) and ... where the current target is in comparison with the hour or 15 min scope"; "15, 30, 60. i definitely look at half of the hour too"; "this is all just math right now ... we are building our scenario"; "Yes, build it".

A READING, not a rule: it is written into a decision's note (lane field entry_scenario_view) and gates nothing. Nothing here has been shown to pay; the readings are recorded so the labels can be scored on the markets that follow.
Every reading at a moment uses only ticks stamped at or before it.

The parts:
  target     the average of the settlement index over the 60 s before the market's open. studies/results/2026-11-16-target-in-range.txt: Kalshi's recorded strike is that average (median 0.01 bp off over 6,255 markets, 84% exact to 0.1 bp).
  scopes     the 15, 30 and 60 minutes before the open. In each: where the target sits in the index's range (0 = at the low, 1 = at the high) and how far the index travelled across the scope, in the scope's own typical one-minute moves.
  previous   the four markets of this series before this one: how far the index finished from each target, in typical moves, and how many times the favorite changed.
  cushion    now: how far the spot sits on the side's side of the target, in units of what the index typically moves in the time left; and where the spot sits in each scope's range ending now.
"""
from __future__ import annotations

import bisect
import math
from typing import Any, Sequence

SCOPES = (15, 30, 60)  # minutes; the user's three views
TARGET_SECONDS = 60.0  # the target is the index's average over this long before the open (structural: Kalshi's own rule)
MINUTE = 60.0


def target(ts: Sequence[float], vs: Sequence[float], opened: float) -> float | None:
    """The average of the index over the 60 s before `opened` (ticks stamped in (opened - 60, opened]). None with under ten ticks there."""
    a, b = bisect.bisect_right(ts, opened - TARGET_SECONDS), bisect.bisect_right(ts, opened)
    if b - a < 10:
        return None
    return sum(vs[a:b]) / (b - a)


def _minute_marks(ts: Sequence[float], vs: Sequence[float], end: float, minutes: int) -> list[float] | None:
    """The index at each minute mark from `minutes` ago to `end` (the last tick at or before each), oldest first. None when a mark has no tick within 90 s before it."""
    out = []
    for k in range(minutes, -1, -1):
        at = end - k * MINUTE
        i = bisect.bisect_right(ts, at) - 1
        if i < 0 or at - ts[i] > 90.0 or vs[i] <= 0:
            return None
        out.append(vs[i])
    return out


def scope(ts: Sequence[float], vs: Sequence[float], end: float, minutes: int, ref: float) -> dict[str, float] | None:
    """One scope ending at `end`: `place` of `ref` in the index's range (0 = at the low, 1 = at the high; outside 0..1 when `ref` is beyond the range), `drift` = the index's travel across the scope in its own typical one-minute moves x sqrt(minutes) (+ = up), `unit` = that typical one-minute move as a fraction of the price. None without a tick near every minute mark or with a flat scope."""
    marks = _minute_marks(ts, vs, end, minutes)
    if marks is None or ref <= 0:
        return None
    a = bisect.bisect_right(ts, end - minutes * MINUTE)
    b = bisect.bisect_right(ts, end)
    lo, hi = min(vs[a:b], default=0.0), max(vs[a:b], default=0.0)
    moves = [math.log(marks[k + 1] / marks[k]) for k in range(len(marks) - 1)]
    unit = math.sqrt(sum(m * m for m in moves) / len(moves))
    if hi <= lo or unit <= 0:
        return None
    return {"place": (ref - lo) / (hi - lo), "drift": math.log(marks[-1] / marks[0]) / (unit * math.sqrt(minutes)), "unit": unit}


def at_open(ts: Sequence[float], vs: Sequence[float], opened: float) -> dict[str, Any] | None:
    """The scenario fixed at the market's open: the target and its place and the drift in each of the three scopes before it. None when the target cannot be read; a scope that cannot be read is left out."""
    t = target(ts, vs, opened)
    if t is None:
        return None
    scopes = {m: sc for m in SCOPES if (sc := scope(ts, vs, opened, m, t)) is not None}
    return {"target": t, "scopes": scopes}


def cushion(ts: Sequence[float], vs: Sequence[float], now: float, tgt: float, side: str, left: float, unit: float) -> float | None:
    """How far the spot sits on the side's side of the target, in units of what the index typically moves in the time left (`unit` per sqrt-minute, from a scope). None with a stale spot (over 20 s), no unit or no time left."""
    i = bisect.bisect_right(ts, now) - 1
    if i < 0 or now - ts[i] > 20.0 or vs[i] <= 0 or tgt <= 0 or unit <= 0 or left <= 0:
        return None
    return (1.0 if side == "yes" else -1.0) * math.log(vs[i] / tgt) / (unit * math.sqrt(left / MINUTE))


def flips(mids: Sequence[float]) -> int:
    """How many times the favorite changed along a market's YES mids: a side is the favorite once its mid is 0.55 or over and stays it until the other side's is (a wobble around 0.50 is not a flip)."""
    fav, n = None, 0
    for m in mids:
        now = "yes" if m >= 0.55 else "no" if m <= 0.45 else None
        if now and fav and now != fav:
            n += 1
        fav = now or fav
    return n


def margin(ts: Sequence[float], vs: Sequence[float], close: float, strike: float, unit: float) -> float | None:
    """How far a finished market's index ended from its target, in typical moves over the market's 15 minutes (+ = above the target): the 60 s average before the close against the strike."""
    end = target(ts, vs, close)
    if end is None or strike <= 0 or unit <= 0:
        return None
    return math.log(end / strike) / (unit * math.sqrt(15.0))


def label(opening: dict[str, Any], cush: float | None) -> str:
    """A short name for the scenario: where the target sits in the widest scope read ("low" under 0.2 of the range, "high" over 0.8, "mid" between) and how wide the cushion is ("against" under 0, "thin" under 0.5 typical remaining moves, "fair" to 1.5, "wide" over). The cut-offs name the reading; they decide nothing."""
    sc = opening["scopes"]
    widest = max(sc) if sc else None
    place = "n/a" if widest is None else "low" if sc[widest]["place"] < 0.2 else "high" if sc[widest]["place"] > 0.8 else "mid"
    cu = "n/a" if cush is None else "against" if cush < 0 else "thin" if cush < 0.5 else "fair" if cush < 1.5 else "wide"
    return f"target {place} in the {widest or '?'} min, cushion {cu}"


def note(opening: dict[str, Any], cush: float | None, now_place: dict[int, float], prev: Sequence[tuple[float | None, int | None]]) -> str:
    """The reading as one bracketed tag for a decision's note."""
    sc = opening["scopes"]
    place = " ".join(f"{m}:{sc[m]['place']:.2f}" for m in SCOPES if m in sc) or "n/a"
    drift = " ".join(f"{m}:{sc[m]['drift']:+.1f}" for m in SCOPES if m in sc) or "n/a"
    spot = " ".join(f"{m}:{now_place[m]:.2f}" for m in SCOPES if m in now_place) or "n/a"
    was = " ".join(f"{'n/a' if mg is None else format(mg, '+.1f')}/{'?' if fl is None else fl}" for mg, fl in prev) or "n/a"
    return (f"[scenario: {label(opening, cush)} | target in range {place} | drift {drift} | cushion {'n/a' if cush is None else format(cush, '+.2f')} "
            f"| spot in range now {spot} | last markets margin/flips {was}]")
