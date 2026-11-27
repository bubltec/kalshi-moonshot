"""The wave lane's dip-and-peak buy on PriskP's curve reading (buy step `wave_dip`; 2026-11-27, PAPER on the wave_shape lane).

User, 2026-11-26: "I want to utilize the curve logic in priskp, which identifies curves really well"; "right on the dip as it starts to turn we buy Yes. The same for peaks, right as a peak crests we buy No"; "the priskp lane is for favorites and
not for scalping, that's what the wave lane is for". PriskP WAITS at a dip or a peak (`wavefacts.curve`: the swing runs against the bet); this step BUYS there, the cheap side: YES while the spot's swing runs down, NO while it runs up.

The rule, all of it read from `wavefacts.curve` (the walk back from now to the window's open, in the market's own typical minutes):
  the market is a CURVE (waves in the hour before the open and the target within the usual wave travel for the time left);
  the current swing runs AGAINST the side (a dip for YES, a peak for NO);
  that swing has run at least wavefacts.TREND_MIN typical minutes (a real dip or peak, not a wiggle).
Once per swing: a swing has its buy the first time the rule holds; a later look in the same swing is refused until the reading has left 'against' for the side (the swing turned) and a new one has begun. A resting buy's own re-ask (ctx.resting)
stands while the swing still runs against the side in a curve market, and is out when it has turned (the price is then running away from the order) or the market is no longer a curve.

Evidence (in sample, 11-24 12:00 UTC to 11-25, studies/results/2026-11-26-wave-scalp-entry.txt): on the same swings at 0.18-0.40 the dip and the leg's extreme beat the confirmed turn; bought in the dip, half the bets later traded at 0.60 or
more; the round trip is positive before the spread and fees and negative crossing the market on both legs, so the lane's resting buy and resting sale decide whether it pays. The price band is the lane's `price` step, not this step.

Memory only (the tick history through wavefacts); stdlib only. The fires are per (lane, index, window, side)."""
from __future__ import annotations

import threading
from typing import Any

SAME_ASK_S = 1.0  # a second look inside this many seconds of the fire is the same decision (the review, then the evaluation), not a new entry: structural, the reading is per second

_lock = threading.Lock()
_swings: dict[tuple[str, str, int, str], dict[str, Any]] = {}  # (lane, index, window open, side) -> {"against": is the swing against the side at the last look, "fired": the time this swing had its buy or None}


def clear() -> None:
    with _lock:
        _swings.clear()


def judge(lane: str, index: str, opened: float, side: str, now: float, c: dict[str, Any] | None, resting: bool = False) -> tuple[bool, str]:
    """(buy?, the sentence for the decision's note) for one side at one look, given the curve reading `c` (wavefacts.curve) or None when it cannot be read. Records the swing's fire."""
    from . import wavefacts

    key = (lane, index, round(opened), side)
    what = "dip" if side == "yes" else "peak"
    if c is None:
        return False, "the curve cannot be read yet (the hour before the open or the path since it is not in memory)"
    told = (f"swing {c['swing_u']:.2f} typical minutes {'down' if side == 'yes' else 'up'} ({c['class']} the {side.upper()}), the swing before it {c['prev_u']:.2f}, gave back {c['gb_u']:.2f}; the big leg has run "
            f"{'n/a' if c['age'] is None else format(c['age'], '.2f')} of a usual one and travelled {'n/a' if c['travel'] is None else format(c['travel'], '.2f')} of a usual wave{' (at its extreme)' if c['at_extreme'] else ''}; "
            f"the target is {c['reach']:.2f} of the usual wave travel for the time left away ({c['regime']})")
    against = c["class"] == "against"
    with _lock:
        if len(_swings) > 4096:
            for k in [k for k in _swings if k[2] < round(opened) - 3600]:
                del _swings[k]
        st = _swings.setdefault(key, {"against": False, "fired": None})
        if c["class"] != "none":  # 'none' (no swing of half a typical minute yet) between two readings does not end a swing
            if not against:
                st["fired"] = None  # the swing has turned for this side (or runs with it): the next dip or peak is a new one
            st["against"] = against
        fired = st["fired"]
    if c["regime"] != "curve":
        return False, f"not a curve market now, no {what} to buy: {told}"
    if not against:
        return False, f"no {what}: the swing does not run against the {side.upper()}: {told}"
    if resting:
        if fired is not None:
            return True, f"the {side.upper()} {what} fired {now - fired:.0f}s ago and its order stands: the swing still runs against the side in a curve market; {told}"
        return False, f"no {what} buy stands for this swing; {told}"
    if fired is not None:
        if 0.0 <= now - fired <= SAME_ASK_S:
            return True, f"wave {what} (fired): buys {side.upper()}; {told}"
        return False, f"this {what} had its buy {now - fired:.0f}s ago (once per swing); {told}"
    if c["swing_u"] < wavefacts.TREND_MIN:
        return False, f"the {what} has run {c['swing_u']:.2f} typical minutes, under {wavefacts.TREND_MIN:g}: not a real {what} yet; {told}"
    with _lock:
        _swings[key]["fired"] = now
    return True, f"wave {what} (fired): buys {side.upper()}; {told}"


def admits(shot: Any, now: float, strike: float | None, resting: bool = False) -> tuple[bool, str]:
    """The buy step's answer for one side. Memory only."""
    from . import binaryrisk, wavefacts

    series = shot.series or shot.ticker.split("-")[0]
    index = binaryrisk.curve_index(series)
    if not index:
        return False, f"no Kalshi index or perp reference is read for {series}"
    if not strike:
        return False, "the market's target is not in memory yet"
    opened = round((now + shot.hours_to_close * 3600.0) / 900.0) * 900.0 - 900.0
    c = wavefacts.curve(index, opened, now, float(strike), shot.side, max(opened + 900.0 - now, 1.0))
    return judge(str(shot.lane), index, opened, shot.side, now, c, resting)
