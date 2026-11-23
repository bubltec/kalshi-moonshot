"""The wave flag: a candidate buy beside the wave top (buy step `wave_flag`, 2026-11-23), built from the flag designed against the shuffle test in scripts/wave_flag_lab.py (`flag_fires`).

Why (2026-11-23, user: "designing the new buy flag against this shuffle test"): the wave-top detector fired on 26.6% of real paths and on 33.4% of shuffled copies of them (the same one-second changes in random order), so it does not read waves (scripts/wave_null_test.py). This flag fires 11.4% real against 3.7% shuffled at M=2 and 19.1% against 14.8% at M=1 (scripts/wave_flag_lab.py). Its entries' scalp rate was no better than a random point and M=1's hold result (+0.52 +- 0.31 a bet) is not significant: a lead for paper, not a rule (CLAUDE.md rule 5).

Everything is in the spot's own typical 10 seconds, sigma10: the root-mean-square 10-second change of the raw spot, sampled every 10 s over the hour before the market's open. The spot is Kalshi's own (binaryrisk.curve_index: a coin's settlement index, a commodity's perp reference). The target is on that curve's scale, as the lab read it (`target`): a coin's market strike; a commodity's perp value at the open (Kalshi's commodity strike is on another price and sits off the perp by a basis, so it is not used; the wave top does use it, an open item in docs/architecture.md).

  walk     the raw spot, second by second, from LOOK seconds before the open (the legs before the open count toward M).
  leg      runs from a pivot to its extreme. It is CONFIRMED when the spot comes off the extreme by at least max(turn x the swing, sigma10); the next leg starts at that extreme.
  size     a leg is SIGNIFICANT when its swing is at least z x sigma10 x sqrt(max(duration, 5) / 10): judged against what a random walk moves in that time.
  flag     at a significant leg's confirmation, when at least M significant legs (this one included) were confirmed in the last `window` seconds and the whole leg stayed on one side of the target: a rise whose pivot and current spot are both over the strike is a PEAK (the bet is NO); a fall whose pivot and current spot are both under it is a DIP (the bet is YES).

Lane fields (lanes.py): wave_flag_z (3.0), wave_flag_turn (0.35), wave_flag_legs (M, 1), wave_flag_window (180 s); the defaults are the lab's, fixed before its run.

The lab fires in the confirmation second only. The entry thread looks at a market at most once every entry_min_gap_seconds (config, 5 s), so a flag that lived one second would be missed by most looks: the step keeps the flag open for `hold` seconds after its confirmation (the step passes the look gap), unless a newer leg has been confirmed since. With hold 0 the step's answer at every second is the lab's (scripts/wave_flag_parity.py). The price band, the entry window and every other judgement are the lane's other steps (`price`, `entry_window`).

The path is gridded as the lab grids it: each second carries the last tick at or before it, and the path's first second carries the last tick before it however old (a commodity's daily session break leaves the perp silent for about an hour; the lab, and so this step, read that stretch as flat, which lowers sigma10 in the hour after the break).

Memory only: the tick history (ticks.history) and the target in memory. Nothing here reads the database. stdlib only.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Any

from .wavetop import grid

LEAD = 3700  # the path starts this long before the open (index LEAD is the open), as wavetop.read and the lab lay it out
SPAN = 3600  # sigma10 is read over this long before the open
STEP10 = 10  # sigma10's step: the change over 10 s, sampled every 10 s
LOOK = 300  # legs are walked from this long before the open
MIN_DUR = 5  # a leg's duration is read as at least this many seconds (a one-tick jump is not given an unbounded size)


@dataclass
class Leg:
    k: int  # the second (path index) the leg was confirmed in
    d: int  # +1 a rise to a peak, -1 a fall to a dip
    pk: int  # the pivot's second
    pv: float  # the pivot's value
    ek: int  # the extreme's second
    ev: float  # the extreme's value
    v: float  # the spot at the confirmation
    swing: float  # (extreme - pivot) in the leg's direction, in sigma10
    dur: int  # max(extreme - pivot, MIN_DUR) seconds
    z: float  # the swing over what a random walk moves in dur: swing / (sigma10 x sqrt(dur / 10))
    significant: bool


@dataclass
class Flag:
    d: int
    t: int  # seconds after the open the leg was confirmed
    age: int  # seconds since the confirmation
    leg: Leg
    recent: int  # significant legs confirmed in the window, this one included
    sig10: float

    @property
    def side(self) -> str:
        return "no" if self.d == 1 else "yes"


def sigma10(sp: list[float], ia: int) -> float:
    """The root-mean-square 10-second change of the path over the SPAN seconds before index ia, sampled every 10 s (the lab's `sigma10`, operation for operation)."""
    xs = range(ia - SPAN + STEP10, ia, STEP10)
    return math.sqrt(sum((sp[x] - sp[x - STEP10]) ** 2 for x in xs) / max(1, len(xs)))


def walk(sp: list[float], ia: int, end: int, sig10: float, z_min: float, turn: float) -> list[Leg]:
    """Every leg confirmed from LOOK seconds before the open through index `end`, in order. The loop is the lab's `flag_fires` walk, step for step."""
    k0 = ia - LOOK
    lo = hi = sp[k0]
    klo = khi = k0
    d = 0
    pk, pv = k0, sp[k0]
    ek, ev = k0, sp[k0]
    out: list[Leg] = []
    for k in range(k0 + 1, end + 1):
        v = sp[k]
        if d == 0:
            if v < lo:
                lo, klo = v, k
            if v > hi:
                hi, khi = v, k
            if v - lo >= sig10:
                d, pk, pv, ek, ev = 1, klo, lo, k, v
            elif hi - v >= sig10:
                d, pk, pv, ek, ev = -1, khi, hi, k, v
            continue
        if (v - ev) * d > 0:
            ek, ev = k, v
            continue
        draw = (ev - v) * d
        move = (ev - pv) * d
        if draw >= max(turn * move, sig10):
            dur = max(ek - pk, MIN_DUR)
            z = move / (sig10 * math.sqrt(dur / 10.0))
            out.append(Leg(k, d, pk, pv, ek, ev, v, move / sig10, dur, z, z >= z_min))
            pk, pv, ek, ev, d = ek, ev, k, v, -d
    return out


def fires_at(legs: list[Leg], i: int, strike: float, m: int, window: float) -> tuple[str | None, int, str]:
    """Whether the confirmation of legs[i] is a flag: (side or None, the significant legs in the window, why not)."""
    leg = legs[i]
    recent = sum(1 for x in legs[: i + 1] if x.significant and leg.k - x.k <= window)
    if not leg.significant:
        return None, recent, "not significant"
    if leg.d == 1 and not (leg.pv > strike and leg.v > strike):
        return None, recent, "the rise did not stay over the target"
    if leg.d == -1 and not (leg.pv < strike and leg.v < strike):
        return None, recent, "the fall did not stay under the target"
    if recent < m:
        return None, recent, "too few significant legs"
    return ("no" if leg.d == 1 else "yes"), recent, ""


def _leg_text(leg: Leg) -> str:
    return f"a {'rise' if leg.d == 1 else 'fall'} of {leg.swing:.1f} typical 10 s over {leg.dur}s (z {leg.z:.2f})"


def read(T: list[float], V: list[float], opened: float, now: float, strike: float, z_min: float, turn: float, m: int, window: float, hold: float = 0.0) -> tuple[Flag | None, str]:
    """The flag at `now` for a market that opened at `opened` with target `strike`, from the spot's ticks (T ascending, from before opened - LEAD through now). (Flag, "") or (None, why not)."""
    a = int(opened) - LEAD
    ia = LEAD
    sp = grid(T, V, a, int(now) + 1)
    if len(sp) <= ia:
        return None, "the market has not opened"
    if None in sp[ia - SPAN :]:
        return None, "the spot's history does not cover the hour before the open, so its typical 10 s cannot be read"
    sig = sigma10(sp, ia)  # type: ignore[arg-type]
    if sig <= 0:
        return None, "the spot did not move in the hour before the open, so its typical 10 s cannot be read"
    end = len(sp) - 1
    legs = walk(sp, ia, end, sig, z_min, turn)  # type: ignore[arg-type]
    unit = f"typical 10 s {sig:.6g}"
    if not legs:
        return None, f"no leg confirmed since {LOOK}s before the open ({unit})"
    last = legs[-1]
    age = end - last.k
    if age > hold:
        return None, f"no leg confirmed now: the last, {_leg_text(last)}, was confirmed {age}s ago ({unit})"
    side, recent, why = fires_at(legs, len(legs) - 1, strike, m, window)
    told = f"{_leg_text(last)} confirmed {age}s ago at {(last.k - ia) / 60.0:.1f} min, {recent} significant leg(s) in the last {window:g}s ({unit})"
    if side is None:
        if why == "not significant":
            return None, f"{told}: under z {z_min:g}, not significant"
        if why == "too few significant legs":
            return None, f"{told}: needs {m}"
        return None, f"{told}: pivot {last.pv:.6g} and spot {last.v:.6g} against the target {strike:.6g}, {why}"
    return Flag(last.d, last.k - ia, age, last, recent, sig), f"{told}, pivot {last.pv:.6g} and spot {last.v:.6g} both {'over' if last.d == 1 else 'under'} the target {strike:.6g}"


_memo: dict[tuple[Any, ...], tuple[Flag | None, str]] = {}
_memo_lock = threading.Lock()


def read_index(index: str, opened: float, now: float, strike: float, z_min: float, turn: float, m: int, window: float, hold: float) -> tuple[Flag | None, str]:
    """`read` on the tick memory, kept for the second it was read in (both sides and every lane with the same settings ask the same question)."""
    from . import ticks as ticks_mod

    key = (index, int(opened), int(now), strike, z_min, turn, m, window, hold)
    with _memo_lock:
        if key in _memo:
            return _memo[key]
    start = opened - LEAD
    got = ticks_mod.history.window(index, start, now)
    if not got or not got[0]:
        out: tuple[Flag | None, str] = (None, f"no tick history in memory for {index} back to an hour before the open")
    else:
        T, V = got
        if T[0] > start:  # the path's first second carries the last tick before it, however old (a commodity's session break): the lab's grid does the same
            prev = ticks_mod.history.last_at_or_before(index, start)
            if prev is not None:
                T, V = [prev[0], *T], [prev[1], *V]
        out = read(T, V, opened, now, strike, z_min, turn, m, window, hold)
    with _memo_lock:
        if len(_memo) > 400:
            _memo.clear()
        _memo[key] = out
    return out


def target(series: str, index: str, opened: float, market_strike: float | None) -> tuple[float | None, str]:
    """The target on the curve's own scale, as the lab read it (scripts/study_cache_build.py, binaryrisk._curve): a coin's market strike (its settlement index is what Kalshi's strike is written on); a commodity's perp reference value at the open (binaryrisk.open_strike: the first tick within 25 s of it), because Kalshi's commodity strike is on another price than the perp and sits off it by a basis. (target, "") or (None, why)."""
    from . import binaryrisk
    from . import ticks as ticks_mod
    from .kalshiws import index_for_series

    if index_for_series(series):
        return (float(market_strike), "") if market_strike else (None, "the market's target is not in memory yet")
    got = ticks_mod.history.window(index, opened, opened + 30.0)
    got_strike = binaryrisk.open_strike(got[0], got[1], opened + 900.0) if got and got[0] else None
    return (got_strike, "") if got_strike else (None, f"the perp reference {index} has no tick within 25 s of the open in memory, so the target on its scale is unknown")


def admits(s: Any, shot: Any, spec: Any, now: float, strike: float | None) -> tuple[bool, str]:
    """The buy step's answer for one side: (buy?, the sentence for the decision's note). `strike` is the market's own (Kalshi's) strike; `target` decides what the flag compares with. Memory only."""
    from . import binaryrisk

    series = shot.series or shot.ticker.split("-")[0]
    index = binaryrisk.curve_index(series)
    if not index:
        return False, f"no Kalshi index or perp reference is read for {shot.series}"
    opened = round((now + shot.hours_to_close * 3600.0) / 900.0) * 900.0 - 900.0
    strike, why = target(series, index, opened, strike)
    if strike is None:
        return False, why
    hold = float(getattr(s, "entry_min_gap_seconds", 0.0) or 0.0)
    f, why = read_index(index, opened, now, strike, spec.wave_flag_z, spec.wave_flag_turn, spec.wave_flag_legs, spec.wave_flag_window, hold)
    if f is None:
        return False, f"no wave flag: {why}"
    told = f"{'peak' if f.d == 1 else 'dip'}: {why}"
    if shot.side != f.side:
        return False, f"{told}: the bet is {f.side.upper()}, not this side"
    return True, f"wave flag: {told}; buys {f.side.upper()}"
