"""The wave lane's dip-and-peak buy on PriskP's curve reading (buy step `wave_dip`; 2026-11-27, PAPER on the wave_shape lane).

User, 2026-11-26: "I want to utilize the curve logic in priskp, which identifies curves really well"; "right on the dip as it starts to turn we buy Yes. The same for peaks, right as a peak crests we buy No"; "the priskp lane is for favorites and
not for scalping, that's what the wave lane is for". PriskP WAITS at a dip or a peak (`wavefacts.curve`: the swing runs against the bet); this step BUYS there, the cheap side: YES as a dip starts to turn, NO as a peak crests.

The rule, all of it read from `wavefacts.curve` (the walk back from now to the window's open, in the market's own typical minutes):
  the market is a CURVE (waves in the hour before the open and the target within the usual wave travel for the time left);
  the current swing runs AGAINST the side (a dip for YES, a peak for NO) and has run at least wavefacts.TREND_MIN typical minutes (a real dip or peak, not a wiggle);
  it is STARTING TO TURN: the spot has come back START_GB typical minutes from the swing's extreme (the reader's own turn size), short of the 0.5 that makes the reading call a new swing;
  it is a NEW dip or peak for the lane: once per swing, and not a swing that was already running when the lane last sold a bet on this market.
A resting buy's own re-ask (ctx.resting) stands while that swing is still the current one, or the swing that followed it runs with the side and is still young (under TREND_MIN), in a curve market; it is out when the spot has gone back to the
swing's extreme (the turn did not hold), when the new swing has run a typical minute (the price has left), or when the market is no longer a curve.

WHY IT CHANGED (2026-11-27, the lane's 145 live bets of 01:30 to 07:15 ET, studies/results/2026-11-27-wave-dip-live-review.txt). The first version bought as soon as the swing against the side had run a typical minute, with nothing about its
turning (my reading of the user's words, and wrong). On the live bets that did two things: (1) it bought while the price was still falling, and the lane's give-up sold 66 of those at the low a median 78 s later (-$72); (2) THE REVERSAL: the
rise that made the turn sale take a YES gain was, for the step, a peak for the NO, so it bought NO seconds after selling YES, into a move that went on to 0.90 at the median (COPPER 04:28 and XRP 04:51 bought the other side 5 s after a sale;
ZEC 05:45 placed a YES buy 14 s after selling NO; user: "It's like rebuys don't have any logic applied", "it follows some weird reversal logic on sell/buy with rebuying"). 27 re-buys won 8.

Memory only (the tick history through wavefacts, the bet book in memory for the lane's last sale); stdlib only. The fires are per (lane, index, window, side)."""
from __future__ import annotations

import threading
from typing import Any

START_GB = 0.2  # typical minutes back from the swing's extreme that count as starting to turn: waveturn.GB, the reader's own turn size (the reading calls a new swing at 0.5)
SAME_ASK_S = 1.0  # a second look inside this many seconds of the fire is the same decision (the review, then the evaluation), not a new entry: structural, the reading is per second

_lock = threading.Lock()
_fires: dict[tuple[str, str, int, str], dict[str, Any]] = {}  # (lane, index, window open, side) -> {"swing": the fired swing's start (wavefacts.leg's swing_start, a time), "at": when it fired}


def clear() -> None:
    with _lock:
        _fires.clear()


def _told(c: dict[str, Any], side: str) -> str:
    return (f"swing {c['swing_u']:.2f} typical minutes {'down' if (side == 'yes') == (c['class'] == 'against') else 'up'} ({c['class']} the {side.upper()}), the swing before it {c['prev_u']:.2f}, gave back {c['gb_u']:.2f}; the big leg has run "
            f"{'n/a' if c['age'] is None else format(c['age'], '.2f')} of a usual one and travelled {'n/a' if c['travel'] is None else format(c['travel'], '.2f')} of a usual wave{' (at its extreme)' if c['at_extreme'] else ''}; "
            f"the target is {c['reach']:.2f} of the usual wave travel for the time left away ({c['regime']})")


def judge(lane: str, index: str, opened: float, side: str, now: float, c: dict[str, Any] | None, resting: bool = False, last_exit: float | None = None) -> tuple[bool, str]:
    """(buy?, the sentence for the decision's note) for one side at one look, given the curve reading `c` (wavefacts.curve) or None when it cannot be read, and when the lane last sold a bet on this market (`last_exit`, or None). Records the swing's fire."""
    from . import wavefacts

    key = (lane, index, round(opened), side)
    what = "dip" if side == "yes" else "peak"
    if c is None:
        return False, "the curve cannot be read yet (the hour before the open or the path since it is not in memory)"
    told = _told(c, side)
    swing = c.get("swing_start")
    with _lock:
        if len(_fires) > 4096:
            for k in [k for k in _fires if k[2] < round(opened) - 3600]:
                del _fires[k]
        fired = _fires.get(key)
    if c["regime"] != "curve":
        return False, f"not a curve market now, no {what} to buy: {told}"
    if resting:
        if fired is None:
            return False, f"no {what} buy stands for this market; {told}"
        ago = now - fired["at"]
        if c["class"] == "against" and swing == fired["swing"]:
            if c["gb_u"] < START_GB - 1e-9 and ago > SAME_ASK_S:
                return False, f"the {what} the order was placed on has gone back to its extreme (gave back {c['gb_u']:.2f}, under {START_GB:g}): the turn did not hold; {told}"
            return True, f"the {side.upper()} {what} fired {ago:.0f}s ago and its order stands: the {what} is still turning; {told}"
        if c["class"] in ("inside", "turned") and c["swing_u"] < wavefacts.TREND_MIN:
            return True, f"the {side.upper()} {what} fired {ago:.0f}s ago and its order stands: the {what} has turned and the new swing is {c['swing_u']:.2f} typical minutes, under {wavefacts.TREND_MIN:g}; {told}"
        if c["class"] == "none":
            return True, f"the {side.upper()} {what} fired {ago:.0f}s ago and its order stands (no swing read at this second); {told}"
        return False, f"the {what} the order was placed on is over (the price has left or a new swing runs against the side); {told}"
    if c["class"] != "against":
        return False, f"no {what}: the swing does not run against the {side.upper()}: {told}"
    if fired is not None and fired["swing"] == swing:
        if 0.0 <= now - fired["at"] <= SAME_ASK_S:
            return True, f"wave {what} (fired): buys {side.upper()}; {told}"
        return False, f"this {what} had its buy {now - fired['at']:.0f}s ago (once per swing); {told}"
    if c["swing_u"] < wavefacts.TREND_MIN:
        return False, f"the {what} has run {c['swing_u']:.2f} typical minutes, under {wavefacts.TREND_MIN:g}: not a real {what} yet; {told}"
    if last_exit is not None and swing is not None and float(swing) <= last_exit:
        return False, f"this {what} was already running when the lane sold its bet on this market {now - last_exit:.0f}s ago: the move that closed a bet is not a new entry, a new {what} must begin after the sale; {told}"
    if c["gb_u"] < START_GB - 1e-9:
        return False, f"the {what} has not started to turn: {c['gb_u']:.2f} typical minutes back from its extreme, under {START_GB:g}; {told}"
    with _lock:
        _fires[key] = {"swing": swing, "at": now}
    return True, f"wave {what} (fired): buys {side.upper()} as the {what} starts to turn; {told}"


def admits(shot: Any, now: float, strike: float | None, resting: bool = False, store: Any = None) -> tuple[bool, str]:
    """The buy step's answer for one side. Memory only: the lane's last sale on this market comes from the bet book in memory (store.last_exit_ts), asked only at a decision (the review has no books)."""
    from . import binaryrisk, wavefacts

    series = shot.series or shot.ticker.split("-")[0]
    index = binaryrisk.curve_index(series)
    if not index:
        return False, f"no Kalshi index or perp reference is read for {series}"
    if not strike:
        return False, "the market's target is not in memory yet"
    opened = round((now + shot.hours_to_close * 3600.0) / 900.0) * 900.0 - 900.0
    c = wavefacts.curve(index, opened, now, float(strike), shot.side, max(opened + 900.0 - now, 1.0))
    last_exit = None
    if store is not None and not resting:
        try:
            last_exit = store.last_exit_ts(shot.ticker, shot.lane)
        except Exception:
            last_exit = None
    return judge(str(shot.lane), index, opened, shot.side, now, c, resting, last_exit)
