"""The wave-top reversal scalp: buy the cheap side at the top of a spot wave that is bigger than the waves before it, and sell a bet that dipped once it has come back.

2026-11-21, user (how they trade these markets by hand): "you need to look at the spot curve. I usually bet on when the slope is larger than previous slopes. So it made a larger wave, bet around the top of that one ... You need to watch it go back and forth a little and then the spot will start to go the opposite slope"; "you buy the low price either down on the peak or up on the dip"; "we want the wave history seamless, not cut off at the start of the market". On the exit, of a bet that went against them first: "it will come back down and we might be able to salvage this for a small profit or small loss ... this is a combination of the spot + kalshi price".

The spot is Kalshi's own: a coin's settlement index or a commodity's perp reference (binaryrisk.curve_index). Everything is in the spot's own typical minute (the root-mean-square 60-second change over the hour before the market opened), never in dollars or by asset.

The buy (buy step `wave_top`, lane field wave_top_quantile):
  legs    a zigzag over the spot, second by second, from 3,500 s before the market opened through now (one unbroken history; a wave that began before the open counts in full). A turn is confirmed when the spot comes back R_TURN typical minutes from the running extreme. A leg's slope is its move in typical minutes per minute of its own length.
  wave    the running leg has moved at least MIN_MOVE typical minutes, its slope is above each of the previous PREV legs, and it is at or above the lane's quantile (wave_top_quantile, 0.9) of all the legs confirmed since the history began (at least MIN_LEGS of them): a threshold from the market's own recent readings.
  top     the extreme has stood for at least HOLD_F of the leg's own length, the spot is back at least BACK_U from it and has bounced at least BOUNCE_U off its low since (it went back and forth).
  side    the reversal: NO after an up wave, YES after a down wave.
  trend   a dip bet (YES after a down wave) is not taken when the spot at the open is above its value wave_top_trend_hours before (user: "if the past hour has been rising. i wouldn't give it a whole day"); with no value in memory a dip bet is not taken. Study, 1 h: dip bets after a rising hour lost (-$0.78 a bet on 448, -$1.45 on 104 with the quantile); 3 h did not show it.
The sale (sell step `wave_salvage`): only a bet that has gone under water (its bid at least one of its own typical minutes under the bid at the buy) is sold, once its bid has come at least half a unit off its low, the bid's climb over the last quarter of the time since the low has slowed to a quarter of the run's pace, and the spot has stopped moving the bet's way over that same stretch. A bet that never dipped is held. The user's resting sale at the entry plus RESTING is recorded as "would sell" and never acts: on the same bets it made the result worse.

Evidence (scripts/spot_wave_replay_cache.py, which runs this module's detector; studies/results/2026-11-21-spot-wave-*.txt; 11-09 to 11-21, fee on every leg, 25 contracts): the wave entry beat random cheap-side buys by about $1 a bet (about two standard errors); held to settlement +$556 on 550 bets, +- about $250; the salvage sale +$99; fixed targets and stops all lost; with the resting +0.40 sale added -$168. The settings below and the quantile were chosen after seeing results, and the result is not split by the curve model's shapes: a lead, on paper first (CLAUDE.md rule 5).

Memory only: the tick history and the quote history. Nothing here reads the database. stdlib only.
"""

from __future__ import annotations

import bisect
import math
import threading
from dataclasses import dataclass
from typing import Any

R_TURN = 1.0  # typical minutes back from the extreme that confirm a turn
MIN_MOVE = 2.0  # the wave's least size, in typical minutes
PREV = 3  # the wave's slope is above each of this many legs before it
MIN_LEGS = 8  # legs needed before the quantile is read
HOLD_F = 0.25  # the top has stood for this share of the wave's own length
BACK_U = 0.25  # the spot is back this far from the top ...
BOUNCE_U = 0.10  # ... and has bounced this far off its low since
LOOK = 3500  # seconds of legs before the open
UNIT_SPAN = 3600  # the typical minute is read over this long before the open
RESTING = 0.40  # the user's resting sale, entry plus this: recorded, never acted on


@dataclass
class Wave:
    d: int  # +1 an up wave (the bet is NO), -1 a down wave (the bet is YES)
    slope: float  # typical minutes per minute
    prev_max: float
    cut: float  # the quantile's value among the legs
    legs: int
    move: float  # the wave's size in typical minutes
    back: float
    bounce: float
    unit: float
    ext_ts: float
    ext: float
    spot_open: float

    @property
    def side(self) -> str:
        return "no" if self.d == 1 else "yes"


def grid(T: list[float], V: list[float], a: int, b: int) -> list[float | None]:
    """The last value at or before each whole second in [a, b)."""
    j = bisect.bisect_right(T, a) - 1
    cur = V[j] if j >= 0 else None
    out: list[float | None] = []
    n = len(T)
    for s_ in range(a, b):
        while j + 1 < n and T[j + 1] <= s_:
            j += 1
            cur = V[j]
        out.append(cur)
    return out


def typical_minute(sp: list[float], i_open: int) -> float:
    """The root-mean-square 60-second change over the UNIT_SPAN seconds before index i_open, read every 10 s."""
    ch = [sp[k] - sp[k - 60] for k in range(max(60, i_open - UNIT_SPAN + 60), i_open, 10)]
    return math.sqrt(sum(c * c for c in ch) / len(ch)) if ch else 0.0


class Zig:
    """The zigzag, fed one second at a time. `step` moves it; `signal` answers whether the running leg is a wave top at this second."""

    def __init__(self, k0: int, v0: float, unit: float, quantile: float) -> None:
        self.unit, self.quantile = unit, quantile
        self.legs: list[tuple[int, float]] = []  # (direction, slope) of each confirmed leg
        self.d = 0
        self.piv = (k0, v0)
        self.ext = (k0, v0)

    def step(self, k: int, v: float) -> None:
        d = self.d
        if d == 0:
            if abs(v - self.piv[1]) >= R_TURN * self.unit:
                self.d = 1 if v > self.piv[1] else -1
                self.ext = (k, v)
            return
        if (v - self.ext[1]) * d > 0:
            self.ext = (k, v)
        elif (self.ext[1] - v) * d >= R_TURN * self.unit:  # the turn is confirmed: the running leg is complete
            dur = max(self.ext[0] - self.piv[0], 1)
            self.legs.append((d, abs(self.ext[1] - self.piv[1]) / self.unit / (dur / 60.0)))
            self.piv = self.ext
            self.d = -d
            self.ext = (k, v)

    def signal(self, k: int, sp: list[float]) -> tuple[Wave | None, str]:
        """(the wave, "") when second k is a wave top, else (None, why not)."""
        d, unit = self.d, self.unit
        if d == 0 or len(self.legs) < 2:
            return None, f"only {len(self.legs)} legs so far"
        v = sp[k]
        dur = self.ext[0] - self.piv[0]
        move = abs(self.ext[1] - self.piv[1]) / unit
        if dur < 1 or move < MIN_MOVE:
            return None, f"the running leg is {move:.1f} typical minutes (needs {MIN_MOVE:g})"
        slope = move / (dur / 60.0)
        prev = max(x[1] for x in self.legs[-PREV:])
        if slope <= prev:
            return None, f"the leg's slope {slope:.2f} is not above the last {PREV} legs' {prev:.2f}"
        cut = 0.0
        if self.quantile > 0:
            allv = sorted(x[1] for x in self.legs)
            if len(allv) < MIN_LEGS:
                return None, f"{len(allv)} legs in the history (needs {MIN_LEGS} to read the quantile)"
            cut = allv[min(int(self.quantile * len(allv)), len(allv) - 1)]
            if slope < cut:
                return None, f"the leg's slope {slope:.2f} is under the {self.quantile:.0%} mark of the market's own legs ({cut:.2f})"
        if k - self.ext[0] < HOLD_F * dur:
            return None, f"the top is {k - self.ext[0]}s old (needs {HOLD_F:g} of the wave's {dur}s)"
        low_w = min(x * d for x in sp[self.ext[0] : k + 1])
        back = (self.ext[1] - v) * d / unit
        bounce = (v * d - low_w) / unit
        if back < BACK_U or bounce < BOUNCE_U:
            return None, f"no back and forth yet: {back:.2f} back from the top (needs {BACK_U:g}), {bounce:.2f} off the low (needs {BOUNCE_U:g})"
        return Wave(d, slope, prev, cut, len(self.legs), move, back, bounce, unit, float(self.ext[0]), self.ext[1], 0.0), ""


def read(T: list[float], V: list[float], opened: float, now: float, quantile: float) -> tuple[Wave | None, str]:
    """The wave top at `now` for a market that opened at `opened`, from the spot's ticks (T ascending, covering opened - 3,700 s to now). (None, why) when there is none or the history is short."""
    a = int(opened) - (UNIT_SPAN + 100)
    ia = UNIT_SPAN + 100
    sp = grid(T, V, a, int(now) + 1)
    if len(sp) <= ia or None in sp[ia - UNIT_SPAN :]:
        return None, "the spot's history does not cover the hour before the open"
    unit = typical_minute(sp, ia)  # type: ignore[arg-type]
    if unit <= 0:
        return None, "the spot did not move in the hour before the open"
    z0 = ia - LOOK
    z = Zig(z0, sp[z0], unit, quantile)  # type: ignore[arg-type]
    for k in range(z0 + 1, len(sp)):
        z.step(k, sp[k])  # type: ignore[arg-type]
    w, why = z.signal(len(sp) - 1, sp)  # type: ignore[arg-type]
    if w is None:
        return None, why
    w.ext_ts += a
    w.spot_open = sp[ia]  # type: ignore[assignment]
    return w, ""


_memo: dict[tuple[str, int, int, float], tuple[Wave | None, str]] = {}
_memo_lock = threading.Lock()


def read_index(index: str, opened: float, now: float, quantile: float) -> tuple[Wave | None, str]:
    """`read` on the tick memory, kept for the second it was read in (both sides and every lane of a market ask the same question)."""
    from . import ticks as ticks_mod

    key = (index, int(opened), int(now), quantile)
    with _memo_lock:
        if key in _memo:
            return _memo[key]
    got = ticks_mod.history.window(index, opened - (UNIT_SPAN + 100), now)
    out = (None, f"no tick history in memory for {index} back to an hour before the open") if not got or not got[0] else read(got[0], got[1], opened, now, quantile)
    with _memo_lock:
        if len(_memo) > 400:
            _memo.clear()
        _memo[key] = out
    return out


def admits(s: Any, shot: Any, spec: Any, now: float) -> tuple[bool, str]:
    """The buy step's answer for one side: (buy?, the sentence for the decision's note). Memory only."""
    from . import binaryrisk

    if spec.wave_top_quantile <= 0:
        return False, "the lane sets no quantile (wave_top_quantile)"
    index = binaryrisk.curve_index(shot.series or shot.ticker.split("-")[0])
    if not index:
        return False, f"no Kalshi index or perp reference is read for {shot.series}"
    opened = round((now + shot.hours_to_close * 3600.0) / 900.0) * 900.0 - 900.0
    w, why = read_index(index, opened, now, spec.wave_top_quantile)
    if w is None:
        return False, f"no wave top: {why}"
    told = f"{'up' if w.d == 1 else 'down'} wave of {w.move:.1f} typical minutes, slope {w.slope:.2f} (last {PREV} legs {w.prev_max:.2f}, its {spec.wave_top_quantile:.0%} mark {w.cut:.2f} of {w.legs} legs), {w.back:.2f} back from the top, {w.bounce:.2f} off the low"
    if shot.side != w.side:
        return False, f"{told}: the reversal is {w.side.upper()}, not this side"
    if w.d == -1 and spec.wave_top_trend_hours > 0:
        from . import ticks as ticks_mod

        at = opened - spec.wave_top_trend_hours * 3600.0
        got = ticks_mod.history.window(index, at - 600.0, at)
        if not got or not got[1]:
            return False, f"{told}: a dip bet needs the spot's value {spec.wave_top_trend_hours:g} h before the open, which is not in memory"
        if w.spot_open > got[1][-1]:
            return False, f"{told}: a dip bet after a rising {spec.wave_top_trend_hours:g} h (the spot at the open {w.spot_open:g} is above its {got[1][-1]:g} then) is not taken"
    return True, f"wave top: {told}; buys the reversal"


def side_bids(quotes: list[tuple[float, float, float]], side: str, a: int, b: int) -> list[float | None]:
    """The side's own bid at each second in [a, b) from the market's (time, YES bid, YES ask) rows; the seconds before the first quote carry the first quote."""
    T = [q[0] for q in quotes]
    B = [(q[1] if side == "yes" else 1.0 - q[2]) for q in quotes]
    sb = grid(T, B, a, b)
    first = next((x for x in sb if x is not None), None)
    return [first if x is None else x for x in sb]


def salvage(quotes: list[tuple[float, float, float]], side: str, opened: float, bought: float, now: float, T: list[float], V: list[float]) -> tuple[bool, dict[str, float]]:
    """Whether a bet bought at `bought` is sold now by the salvage rule, and the readings. quotes = the market's (time, YES bid, YES ask) rows; T, V = the spot's ticks."""
    a = int(opened)
    i0, i = int(bought) - a, int(now) - a
    if i0 < 1 or i <= i0:
        return False, {}
    sb = side_bids(quotes, side, a, int(now) + 1)
    if sb[i0] is None:
        return False, {}
    ch = [sb[k] - sb[k - 60] for k in range(60, i0, 5)]  # type: ignore[operator]
    qu = max(math.sqrt(sum(c * c for c in ch) / len(ch)), 0.01) if ch else 0.05  # the side bid's own typical minute before the buy
    b0 = sb[i0]
    armed, lo_b, ilo = False, b0, i0
    for j in range(i0 + 1, i + 1):
        if sb[j] < b0 - qu:  # type: ignore[operator]
            armed = True
        if armed and sb[j] < lo_b:  # type: ignore[operator]
            lo_b, ilo = sb[j], j
    info = {"unit": qu, "entry_bid": b0, "low": lo_b, "armed": float(armed)}
    if not armed or ilo == i or sb[i] - lo_b < 0.5 * qu or i - ilo < 4:  # type: ignore[operator]
        return False, info
    wl = max(int(0.25 * (i - ilo)), 1)
    s_run = (max(sb[i0 : i + 1]) - lo_b) / max(i - ilo, 1)  # type: ignore[type-var,operator]
    s_rec = (sb[i] - sb[i - wl]) / wl  # type: ignore[operator]
    sp = grid(T, V, int(now) - wl, int(now) + 1)
    if sp[0] is None or sp[-1] is None:
        return False, info
    d = 1 if side == "no" else -1  # the wave the bet was against: the bet gains while the spot moves against d
    spot_turn = (sp[-1] - sp[0]) * d >= 0
    info.update({"run": s_run, "recent": s_rec, "window": float(wl), "spot_turn": float(spot_turn)})
    return bool(s_rec <= 0.25 * s_run and spot_turn), info
