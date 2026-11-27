"""The wave lane's low-side buy: the buy step `wave_low` (2026-11-27, user: "Whichever side is low. That's what you buy in the dip or peak"; "buy the low price when it is in a dip or peak. It is so simple. We already have the curve logic").

The rule: on a market that has waves (its pre-open hour had completed legs of one typical minute), when the spot is IN a dip or a peak, buy the side that is cheap there:
  YES in a dip   the spot's current swing (waveturn's zig-zag of 0.5 u on the 30 s median) runs DOWN and has run at least DIP_U typical minutes
  NO in a peak   the same, UP
The module's `price` step says which side is cheap (the lane's 0.18 to 0.40 ask band); this step does not look at the price. There is no confirmation: no turn, no
swing count, no look back for a completed swing. A swing that has run its size is the dip or the peak; the buy goes on it, not after it (the live reader, waveturn.py,
waited for 0.2 u given back and a completed swing, and missed the ETH 12:47 dip: the swing counted as complete only 4 s after the bottom, with YES already at 0.49).
A resting buy is re-asked on every quote: it stands while the swing still runs against the side and is withdrawn when it has turned. The lane buys a swing once (a bet in this swing on this side, from the lane's own bet book; user, 2026-11-27, on re-buys).

What this is NOT: it reads no PriskP verdict, threshold or step. It uses the wave lane's own frozen identification (waveturn.reading, the market's typical minute and the
hour's usual leg), as a function. DIP_U is 1.0 typical minute, the size waveturn.DONE_U already calls a leg rather than a wobble; it is my choice, not scored. Known miss: a
market that is a line in this window but had waves in its hour (SILVER 11:34, 2026-11-27) reads as a dip or peak here too, the pre-open hour being the only wave test.
Memory only: the tick memory and the target in memory. stdlib only."""

from __future__ import annotations

import threading
from typing import Any

from . import waveturn
from .wavetop import grid

DIP_U = 1.0  # the least swing, in typical minutes, that is a dip or a peak (waveturn.DONE_U's size: a leg, not a wobble); chosen by me, not scored

SWING_SLACK = 5.0  # seconds: a bet this close before the swing's start still belongs to it (the zig-zag finds a swing's start a little late)

_lock = threading.Lock()
_reads: dict[tuple[str, int, int], tuple[waveturn.Reading | None, waveturn.Hour, str]] = {}


def read(index: str, opened: float, now: float, K: float) -> tuple[waveturn.Reading | None, waveturn.Hour, str]:
    """(the reading at `now`, the pre-open hour, why it is None) on the tick memory, kept for the second. Changes no wave-turn state."""
    from . import ticks as ticks_mod

    op = round(opened)
    key = (index, op, int(now))
    with _lock:
        if key in _reads:
            return _reads[key]
    h = waveturn._hour(index, op)
    out: tuple[waveturn.Reading | None, waveturn.Hour, str]
    if h.u is None:
        out = (None, h, f"no reading of the pre-open hour: {h.why}")
    else:
        got = ticks_mod.history.window(index, op, now)
        t = int(now) - op
        if not got or not got[0] or t < 1:
            out = (None, h, "no tick since the open in memory")
        else:
            T, V = got
            if T[0] > op:
                prev = ticks_mod.history.last_at_or_before(index, op)
                if prev is not None:
                    T, V = [prev[0], *T], [prev[1], *V]
            win = grid(T, V, op, int(now) + 1)
            if None in win:
                out = (None, h, "the spot's path since the open has a gap at its start")
            else:
                out = (waveturn.reading(win, t, K, h.u, h.leg_u, h.leg_s, h.moves), h, "")  # type: ignore[arg-type]
    with _lock:
        if len(_reads) > 2000:
            _reads.clear()
        _reads[key] = out
    return out


def clear() -> None:
    with _lock:
        _reads.clear()


def facts(r: waveturn.Reading | None, h: waveturn.Hour, side: str, ok: bool) -> dict[str, Any]:
    """The reading as named values for the decision's record (buychain.Check.facts)."""
    out: dict[str, Any] = {"u": None if h.u is None else round(h.u, 8), "hour_leg_u": round(h.leg_u, 3), "hour_leg_s": round(h.leg_s, 1), "side": side, "buy": ok}
    if r is not None:
        out.update({"t": r.t, "dir": r.d, "swing_u": round(r.swing_u, 3), "gb_u": round(r.gb_u, 3), "raw_gb_u": round(r.raw_gb_u, 3), "pos_u": round(r.pos_u, 3), "swing_s": r.swing_s,
                    "leg_u": round(r.leg_u, 3), "size_x": round(r.size_x, 3), "pace_x": round(r.pace_x, 3), "jumpy": round(r.jumpy, 2)})
    return out


def admits_facts(s: Any, shot: Any, now: float, strike: float | None, resting: bool = False, bet_times: list[float] | None = None) -> tuple[bool, str, dict[str, Any] | None]:
    """(buy?, the sentence for the note, the reading's named values or None when the market could not be read). Memory only."""
    from . import binaryrisk, waveflag

    series = shot.series or shot.ticker.split("-")[0]
    index = binaryrisk.curve_index(series)
    if not index:
        return False, f"no Kalshi index or perp reference is read for {series}", None
    opened = round((now + shot.hours_to_close * 3600.0) / 900.0) * 900.0 - 900.0
    K, why = waveflag.target(series, index, opened, strike)
    if K is None:
        return False, why, None
    r, h, bad = read(index, opened, now, K)
    if r is None:
        return False, f"no low-side buy: {bad}", facts(None, h, shot.side, False)
    if h.leg_u <= 0:
        return False, f"no low-side buy: the hour before the open had no completed leg of {waveturn.DONE_U:g}u, not a wave market", facts(r, h, shot.side, False)
    want = -1 if shot.side == "yes" else 1  # YES is the low side in a dip (the spot falling), NO in a peak (rising)
    word = "dip" if shot.side == "yes" else "peak"
    move = "fall" if want == -1 else "rise"
    told = f"{r.t}s in: swing {r.swing_u:.2f}u {'up' if r.d == 1 else 'down' if r.d == -1 else '-'}, {r.gb_u:.2f}u given back, usual leg {h.leg_u:.2f}u, u {h.u:.6g} (target {K:.6g})"
    if r.d != want:
        return False, f"not in a {word} for {shot.side.upper()}: the swing is {'a rise' if r.d == 1 else 'a fall' if r.d == -1 else 'not there yet'}, not a {move}; {told}", facts(r, h, shot.side, False)
    if r.swing_u < DIP_U:
        return False, f"the {move} is {r.swing_u:.2f}u, under {DIP_U:g}u: not yet a {word}; {told}", facts(r, h, shot.side, False)
    start = opened + r.ext_t - r.swing_s  # the second this swing began
    if not resting and bet_times and any(b >= start - SWING_SLACK for b in bet_times):
        return False, f"swing_played: this lane already bought {shot.side.upper()} in this {move} (it began {int(now - start)}s ago); it waits for the next {word}; {told}", facts(r, h, shot.side, False)
    state = "its order stands" if resting else "buys"
    return True, f"in a {word}: the {move} has run {r.swing_u:.2f}u, {shot.side.upper()} is the low side, {state}; {told}", facts(r, h, shot.side, True)
