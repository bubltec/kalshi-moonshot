"""The wave turn: the buy step `wave_turn` (2026-11-24, PAPER), ported from the study on the user's own marked buys (scripts/wave_turn_chop.py and
scripts/wave_target_gate.py, branch wave-turn-chop commit 3232730; studies/results/2026-11-24-wave-turn-chop.txt).

Why (2026-11-24, user): the user marked the seconds they would have bought in 8 wave markets (18 buys) and named 9 'no good buy' markets. A turn
rule found 12 of the 18 buys but fired in every no-good market; the one reading that kept it out of them was the market's pre-open hour: how
often the spot crossed each 15-minute window's opening price (marketprofile.profile_from). With that gate the study hit 9 of 18 buys and stayed
silent in 8 of 9 no-good markets (leave-one-market-out: 13 of 17 markets right). Every setting was chosen after seeing the marks: a lead for
paper (CLAUDE.md rule 5), not retuned here.

The rule, in the market's own typical minute u (the root-mean-square 1 s move over the hour before the open, times sqrt(60); CLAUDE.md rule 1):
  curve   Kalshi's own spot (binaryrisk.curve_index) on a 1 s grid from the open, the last tick carried; smoothed by a centred median of SMOOTH s
          cut at the moment (waveshape.broad), starting at the open
  swing   a zig-zag on the smoothed curve with reversals of ZZ u; the current swing runs from the last confirmed turn to its running extreme
  turn    the swing is at least SWING u and the curve has come back at least GB u from its extreme (before the zig-zag confirms the turn):
          NO after a rise, YES after a fall
  side    NO only when the smoothed curve is not more than BAND u under the target; YES only when not more than BAND u over it
  once    once per swing (its number in the zig-zag and its direction), and not the same side again within SAME_SIDE_GAP s of the last fire
  gate    at least need(t) completed swings of DONE_U u so far (1 before minute 5, 2 at 6, 3 at 8, 4 at 10) (a zig-zag of DONE_U u on the same smoothed curve: the curve has gone DONE_U u one way
          and DONE_U u back); a one-way line has none and is refused. The pre-open hour must be in the tick memory for u, else 'no reading' and no buy.
Logged with every answer (shadow, never gating): the pre-open hour's crossings of the window's open and efficiency, waves (0.5 u turns) so far,
the efficiency of the smoothed curve over the last 5 minutes, the curve's and the swing extreme's distance from the target, the swing size, the
give-back, and the second of the market.

A fire is a yes for the second it is first seen and the entry thread's look gap after it (`hold`, entry_min_gap_seconds), as waveflag.py does, so a
look every few seconds does not miss it; after that the swing has had its buy.

Gate change (2026-11-25, user): the pre-open crossings gate did not hold on 28 new marked markets (it let through 13 of the 16 the user called 'no good buy'
and 12 of 12 picks), and the user's own reading is that the no-good markets are plain trends with no waves, and that this lane is for wave shapes. The gate is now
in-market: a completed swing (the smoothed curve moved DONE_U u one way and DONE_U u back, a zig-zag reversal of DONE_U u) before the buy, need(t) of them ("find
one wave first, then buy the next turn"). On those 28 markets' 36 bets, needing one completed swing passed 23 bets (+19.72) and blocked 13 (-14.82); the cut and
the look were chosen after seeing the marks (studies/results/2026-11-25-wave-swing-gate.txt). The pre-open hour is now read only for u and as shadow readings.

Memory only: the tick history (ticks.history) and the target in memory; the hour's profile is read once per market from memory and kept. stdlib only.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Any

from .waveshape import broad
from .wavetop import grid

SMOOTH = 30  # seconds of the centred median
ZZ = 0.5  # the zig-zag's reversal, in u
SWING = 0.5  # the least swing, in u
GB = 0.2  # given back from the swing's extreme before the buy, in u
BAND = 0.5  # the side gate's band around the target, in u
DONE_U = 1.0  # a completed swing: the curve has gone this far one way and this far back, in u (chosen after seeing the user's 28 marks)
DONE_OFFSET_MIN = 2.0  # the completed swings needed grow with the minutes into the market: one before about minute 5, two at 6, three at 8, four at 10 (swings pile up with time, so a fixed count lets trends in late). From the leave-one-market-out of the swing count on the user's 45 marks (studies/results/2026-11-25-wave-swing-gate.txt): best count 1 at 4 min, 2 at 6, 3 at 8-10; chosen after seeing the marks.
DONE_SLOPE = 0.5  # swings needed per minute after DONE_OFFSET_MIN


def need(t: float) -> int:
    """The completed swings of DONE_U u the lane needs `t` seconds into the market."""
    return max(1, math.floor((t / 60.0 - DONE_OFFSET_MIN) * DONE_SLOPE + 0.25))
SAME_SIDE_GAP = 30  # no second fire on the same side within this many seconds
PRE = 3600  # the hour before the open


def zigzag(xs: list[float], rev: float) -> tuple[list[tuple[int, float, int]], int, int, float]:
    """(confirmed turns [(index, value, +1 peak / -1 dip)], the current direction, the running extreme's index and value) of the study's zig-zag."""
    pts: list[tuple[int, float, int]] = []
    d = 0
    lo = hi = xs[0]
    klo = khi = 0
    ek, ev = 0, xs[0]
    for k in range(1, len(xs)):
        v = xs[k]
        if d == 0:
            if v < lo:
                lo, klo = v, k
            if v > hi:
                hi, khi = v, k
            if v - lo >= rev:
                pts.append((klo, lo, -1))
                d, ek, ev = 1, k, v
            elif hi - v >= rev:
                pts.append((khi, hi, 1))
                d, ek, ev = -1, k, v
            continue
        if (v - ev) * d > 0:
            ek, ev = k, v
        elif (ev - v) * d >= rev:
            pts.append((ek, ev, d))
            d, ek, ev = -d, k, v
    return pts, d, ek, ev


def efficiency(xs: list[float]) -> float:
    path = sum(abs(xs[k] - xs[k - 1]) for k in range(1, len(xs)))
    return abs(xs[-1] - xs[0]) / path if path else 1.0


@dataclass
class Hour:
    """The pre-open hour of one market: the typical minute and the profile (None when it cannot be read)."""

    u: float | None
    cross: float | None
    eff: float | None
    why: str = ""


def hour_of(sp: list[float | None]) -> Hour:
    """The typical minute and the marketprofile reading of a 1 s path covering the hour before the open (index 0 = an hour before it)."""
    from . import marketprofile

    if len(sp) < PRE or None in sp[:PRE]:
        return Hour(None, None, None, "the hour before the open is not fully in the tick memory")
    h: list[float] = sp[:PRE]  # type: ignore[assignment]
    u = math.sqrt(sum((h[k] - h[k - 1]) ** 2 for k in range(1, len(h))) / (len(h) - 1)) * math.sqrt(60)
    if u <= 0:
        return Hour(None, None, None, "the spot did not move in the hour before the open")
    prof = marketprofile.profile_from([float(k) for k in range(PRE)], h, float(PRE - 1), 1.0)
    if not prof:
        return Hour(u, None, None, "the hour before the open does not give a profile (too few whole windows or ticks)")
    return Hour(u, float(prof["cross"]), float(prof["eff"]))


@dataclass
class Reading:
    t: int  # seconds since the open
    turns: int
    d: int  # +1 rising, -1 falling, 0 no swing yet
    swing_u: float
    gb_u: float
    pos_u: float  # the smoothed curve against the target
    ext_u: float  # the swing's extreme from the target
    eff300: float
    done: int = 0  # completed swings of DONE_U u so far
    ev_u: float = 0.0  # the swing's running extreme against the target, signed, in u


def reading(window: list[float], t: int, K: float, u: float) -> Reading:
    """The turn reading at `t` s after the open from the 1 s path `window` (index 0 = the open) and the target K."""
    sm = broad(window, 0, t, SMOOTH // 2, t)
    pts, d, _ek, ev = zigzag(sm, ZZ * u)
    start = pts[-1][1] if pts else sm[0]
    big, _d, _k, _v = zigzag(sm, DONE_U * u)
    return Reading(t, len(pts), d, (ev - start) * d / u if d else 0.0, (ev - sm[-1]) * d / u if d else 0.0, (sm[-1] - K) / u, abs(ev - K) / u, efficiency(sm[-301:]),
                   max(0, len(big) - 1), (ev - K) / u if d else 0.0)


@dataclass
class Fires:
    """One market's fires so far: the swings that have had theirs, and the last fire (second, side)."""

    done: dict[tuple[int, int], int] = field(default_factory=dict)  # (turns, direction) -> the second it fired
    last: tuple[int, str] | None = None
    stand: dict[str, tuple[int, int, float]] = field(default_factory=dict)  # side -> (second it fired, the swing's direction, its extreme against the target in u): what a resting order stands on


def judge(r: Reading, fires: Fires, hold: float, resting: str | None = None) -> tuple[str | None, str]:
    """The side the turn buys at this reading, or None with why; records a new fire in `fires`. The study's order: the swing, the turn, once per
    swing and the same-side gap, then the side gate; a fire is recorded only when it passes all of them."""
    if resting is not None and resting in fires.stand:
        t0, d0, ev0 = fires.stand[resting]  # a resting buy's own re-ask: its turn stands while the curve has not passed the extreme it turned from (a NO after a rise: no new high), and is then out
        if (r.pos_u - ev0) * d0 < 0:
            return resting, f"the {resting.upper()} turn fired {r.t - t0}s ago and its order stands: the curve has not passed the swing's extreme"
        return None, f"the curve passed the swing's extreme ({r.pos_u:+.2f}u, the {resting.upper()} turn was {ev0:+.2f}u): the turn failed"
    if r.d == 0:
        return None, "no swing yet"
    side = "no" if r.d == 1 else "yes"
    if r.swing_u < SWING:
        return None, f"the swing is {r.swing_u:.2f}u, under {SWING:g}u"
    if r.gb_u < GB:
        return None, f"{r.gb_u:.2f}u given back from the swing's extreme, under {GB:g}u: not turned yet"
    key = (r.turns, r.d)
    if key in fires.done:
        age = r.t - fires.done[key]
        if 0 <= age <= hold:
            return side, f"the turn fired {age}s ago (held for the look gap)"
        return None, f"this swing had its buy at {fires.done[key]}s (once per swing)"
    if fires.last is not None and fires.last[1] == side and r.t - fires.last[0] < SAME_SIDE_GAP:
        return None, f"a {side.upper()} fired {r.t - fires.last[0]}s ago (same side within {SAME_SIDE_GAP}s)"
    if side == "no" and r.pos_u < -BAND:
        return None, f"a NO turn with the curve {r.pos_u:+.2f}u from the target, more than {BAND:g}u under it"
    if side == "yes" and r.pos_u > BAND:
        return None, f"a YES turn with the curve {r.pos_u:+.2f}u from the target, more than {BAND:g}u over it"
    if r.done < need(r.t):
        return None, f"{r.done} completed swings of {DONE_U:g}u so far, the lane needs {need(r.t)} at {r.t}s: a trend has a pull-back at most, a wave comes back (not a wave shape yet)"
    fires.done[key] = r.t
    fires.last = (r.t, side)
    fires.stand[side] = (r.t, r.d, r.ev_u)
    return side, "fired"


_lock = threading.Lock()
_hours: dict[tuple[str, int], Hour] = {}
_fires: dict[tuple[str, int], Fires] = {}
_reads: dict[tuple[str, int, int, str | None], tuple[str | None, str, Reading | None, Hour]] = {}


def _hour(index: str, opened: int) -> Hour:
    from . import ticks as ticks_mod

    key = (index, opened)
    with _lock:
        if key in _hours:
            return _hours[key]
    a = opened - PRE
    got = ticks_mod.history.window(index, a, opened - 1e-6)
    if not got or not got[0]:
        h = Hour(None, None, None, f"no tick history in memory for {index} back to an hour before the open")
    else:
        T, V = got
        if T[0] > a:
            prev = ticks_mod.history.last_at_or_before(index, a)
            if prev is not None:
                T, V = [prev[0], *T], [prev[1], *V]
        h = hour_of(grid(T, V, a, opened))
    with _lock:
        if len(_hours) > 2000:
            _hours.clear()
        if h.u is not None:  # an unreadable hour is read again on a later tick (the warm-up may still be filling the memory)
            _hours[key] = h
    return h


def read_index(index: str, opened: float, now: float, K: float, hold: float, resting: str | None = None) -> tuple[str | None, str, Reading | None, Hour]:
    """(the side the rule buys at `now` or None, why, the reading, the hour) on the tick memory; kept for the second (both sides and every lane
    ask the same question, and a fire is recorded once)."""
    from . import ticks as ticks_mod

    op = round(opened)
    key = (index, op, int(now), resting)
    with _lock:
        if key in _reads:
            return _reads[key]
    h = _hour(index, op)
    out: tuple[str | None, str, Reading | None, Hour]
    if h.u is None:
        out = (None, f"no reading of the pre-open hour: {h.why}", None, h)
    else:
        got = ticks_mod.history.window(index, op, now)
        t = int(now) - op
        if not got or not got[0] or t < 1:
            out = (None, "no tick since the open in memory", None, h)
        else:
            T, V = got
            if T[0] > op:
                prev = ticks_mod.history.last_at_or_before(index, op)
                if prev is not None:
                    T, V = [prev[0], *T], [prev[1], *V]
            win = grid(T, V, op, int(now) + 1)
            if None in win:
                out = (None, "the spot's path since the open has a gap at its start", None, h)
            else:
                r = reading(win, t, K, h.u)  # type: ignore[arg-type]
                with _lock:
                    fires = _fires.setdefault((index, op), Fires())
                    side, why = judge(r, fires, hold, resting)
                out = (side, why, r, h)
    with _lock:
        if len(_reads) > 2000:
            _reads.clear()
        if len(_fires) > 400:
            for k in [k for k in _fires if k[1] < op - 1800]:
                del _fires[k]
        _reads[key] = out
    return out


def clear() -> None:
    with _lock:
        _hours.clear()
        _fires.clear()
        _reads.clear()


def note(r: Reading | None, h: Hour, K: float) -> str:
    """The readings for the decision's note (the gate's reading and the shadow ones)."""
    head = f"pre-open hour: crossings of the window's open {h.cross:.2f} per 15 min, efficiency {h.eff:.3f} (shadow only)" if h.cross is not None else f"pre-open hour: {h.why}"
    if r is None:
        return f"{head} (target {K:.6g})"
    return (f"{head}; {r.t}s in, {SMOOTH}s smoothing: swing {r.swing_u:.2f}u {'up' if r.d == 1 else 'down' if r.d == -1 else '-'}, {r.gb_u:.2f}u given back, "
            f"completed swings of {DONE_U:g}u {r.done} (gate >= {need(r.t)}), waves so far {r.turns}, curve {r.pos_u:+.2f}u and the swing's extreme {r.ext_u:.2f}u from the target, last-5-min efficiency {r.eff300:.2f}, "
            f"u {h.u:.6g} (target {K:.6g})")


def admits(s: Any, shot: Any, now: float, strike: float | None, resting: bool = False) -> tuple[bool, str]:
    """The buy step's answer for one side: (buy?, the sentence for the decision's note). Memory only."""
    from . import binaryrisk, waveflag

    series = shot.series or shot.ticker.split("-")[0]
    index = binaryrisk.curve_index(series)
    if not index:
        return False, f"no Kalshi index or perp reference is read for {series}"
    opened = round((now + shot.hours_to_close * 3600.0) / 900.0) * 900.0 - 900.0
    K, why = waveflag.target(series, index, opened, strike)
    if K is None:
        return False, why
    hold = float(getattr(s, "entry_min_gap_seconds", 0.0) or 0.0)
    side, why, r, h = read_index(index, opened, now, K, hold, shot.side if resting else None)
    told = note(r, h, K)
    if side is None:
        return False, f"no wave turn: {why}; {told}"
    if shot.side != side:
        return False, f"wave turn ({why}): the bet is {side.upper()}, not this side; {told}"
    return True, f"wave turn ({why}): buys {side.upper()}; {told}"
