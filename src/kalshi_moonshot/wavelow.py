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

from . import restbuys, waveturn
from .wavetop import grid

DIP_U = 1.0  # the least swing, in typical minutes, that is a dip or a peak (waveturn.DONE_U's size: a leg, not a wobble); chosen by me, not scored

HALF_U_BOW = 0.15  # 2026-11-27 (user: "All it needs to look for is a half U or the inverse and bet in the middle"): the swing must be BOWED, its path lying this share of its own size past the straight line from its start to now (a fall that dropped fast and is rounding off, the left half of a U; a rise, the left half of a peak). A straight swing is the user's "linear shape". Chosen from the user's description before the replay, not tuned
HALF_U_FLAT = 0.5  # and FLATTENING: over the last quarter of the swing the spot moved at no more than this share of the swing's average pace (the middle of the U). Chosen the same way
LOOKBACK = 1800  # 2026-11-27 (user: "it should have at least 10 mins prior tape to check the curve. 30 would be better"; before that "It's supposed to read at least 10 mins before the marker"): the swing and its shape are read from at least this many seconds before the decision, across the market's open (the index is one continuous series), not from the open alone. Before this the first minutes of a market read a window of seconds: HYPE 22:45, 90 s in, saw only the rise off its own open and called it a peak, when it was the right arm of a 9-minute U that began in the market before. 30 minutes is read when the tape has them, 10 (MIN_LOOKBACK) is the least the step will act on; a replay on 72 markets found the length changes nothing (1u swing -0.07 a dollar from the open, -0.09 from 10 min, -0.08 from 30 min), so this is the user's requirement, not a gain we measured
PREV_MAX = 0.8  # 2026-11-28 (user, on HYPE 22:45 and BTC 07:15: the step bought NO on the bounce off a dip, "it was in a dip, above the target, but still need to buy Yes ... In that case this was a no bet"): a swing is not faded when the swing before it, the other way, was at least this share of its size: it is the recovery of a move at least as big, the right arm of the U, not a peak. Replay on 72 markets read 30 min back: fires with the swing before it under 0.8 of this one took a profit 66-68% of the time and broke even (-0.02 to +0.01 a dollar); at 0.8 and over, 49-56% and -0.15 to -0.18 (the half-U rule -0.27 to -0.29). Line chosen after seeing that table
MIN_LOOKBACK = 600  # the least prior tape the step reads before it will buy: with less in memory (just after a restart) it says no reading
TAPE_SLACK = 30  # seconds: the first tick in memory may be this much later than the window's start
SWING_SLACK = 5.0  # seconds: a bet this close before the swing's start still belongs to it (the zig-zag finds a swing's start a little late)

_lock = threading.Lock()
_reads: dict[tuple[str, int, int], tuple[waveturn.Reading | None, waveturn.Hour, str, dict[str, float] | None]] = {}
_prices: dict[tuple[str, int], dict[str, Any] | None] = {}


def window_start(op: int, now: float, lookback: int = LOOKBACK) -> int:
    """The second the swing is read from: the market's open, or `lookback` seconds before `now` when that is earlier (the first minutes of a market read into the market before)."""
    return min(op, int(now) - lookback)


_starts: dict[tuple[int, int], int] = {}


def read_start(op: int, now: float) -> int:
    """The window start the last read of this second used (30 or 10 minutes back); the 30-minute one when unknown."""
    return _starts.get((op, int(now)), window_start(op, now))


def shape_of(win: list[float], t: int, u: float) -> dict[str, float] | None:
    """The shape of the current swing, from its start (the last confirmed turn of the 0.5 u zig-zag on the 30 s median) to now: `bow` (the largest distance of the smoothed path past the straight line from the swing's start to now, as a share of the swing's size, positive when the path ran ahead of the line: a fall that dropped fast and rounds off, a rise that shot up and rounds off), `flat` (the pace over the last quarter of the swing as a share of its average pace: small is the bottom or top of the arc), `size` (in u), `secs`. None when there is no swing of a few seconds. Memory only."""
    from .waveshape import broad

    sm = broad(win, 0, t, waveturn.SMOOTH // 2, t)
    pts, d, _ek, _ev = waveturn.zigzag(sm, waveturn.ZZ * u)
    if not pts or d == 0:
        return None
    k0 = pts[-1][0]
    n_secs = t - k0
    size = abs(sm[t] - sm[k0])
    if n_secs < 5 or size <= 0:
        return None
    y0, y1 = sm[k0], sm[t]
    bow = max((sm[k] - (y0 + (y1 - y0) * (k - k0) / n_secs)) * d for k in range(k0, t + 1)) / size
    q = max(2, n_secs // 4)
    flat = (abs(sm[t] - sm[t - q]) / q) / (size / n_secs)
    prev = abs(pts[-1][1] - pts[-2][1]) if len(pts) >= 2 else 0.0  # the swing before this one, the other way, in the same units
    return {"bow": round(bow, 3), "flat": round(flat, 3), "size": round(size / u, 3), "secs": float(n_secs), "prev_ratio": round(prev / size, 3)}


def read(index: str, opened: float, now: float, K: float) -> tuple[waveturn.Reading | None, waveturn.Hour, str, dict[str, float] | None]:
    """(the reading at `now`, the pre-open hour, why it is None, the swing's shape) on the tick memory, kept for the second. Changes no wave-turn state."""
    from . import ticks as ticks_mod

    op = round(opened)
    key = (index, op, int(now))
    with _lock:
        if key in _reads:
            return _reads[key]
    h = waveturn._hour(index, op)
    out: tuple[waveturn.Reading | None, waveturn.Hour, str, dict[str, float] | None]
    if h.u is None:
        out = (None, h, f"no reading of the pre-open hour: {h.why}", None)
    else:
        ws, got = op, None
        for lb in (LOOKBACK, MIN_LOOKBACK):  # 30 minutes of prior tape when memory has them, else at least 10
            ws = window_start(op, now, lb)
            got = ticks_mod.history.window(index, ws, now)
            if got and got[0] and got[0][0] <= ws + TAPE_SLACK:
                break
            got = None
        t = int(now) - ws
        if got is None or t < 1:
            out = (None, h, f"the tape in memory does not reach back {MIN_LOOKBACK // 60} minutes before the decision yet", None)
        else:
            T, V = got
            if T[0] > ws:
                prev = ticks_mod.history.last_at_or_before(index, ws)
                if prev is not None:
                    T, V = [prev[0], *T], [prev[1], *V]
            win = grid(T, V, ws, int(now) + 1)
            _starts[(op, int(now))] = ws
            if None in win:
                out = (None, h, "the spot's path in the window has a gap at its start", None)
            else:
                out = (waveturn.reading(win, t, K, h.u, h.leg_u, h.leg_s, h.moves), h, "", shape_of(win, t, h.u))  # type: ignore[arg-type]
    with _lock:
        if len(_reads) > 2000:
            _reads.clear()
            _starts.clear()
        _reads[key] = out
    return out


PRICE_ZZ = 0.5  # the price curve's zig-zag reversal, in the price's own typical minutes (waveturn.ZZ's size); SHADOW, not scored
PRICE_MIN_S = 120  # seconds of quotes before the price curve has a typical minute of its own


def price_curve(ticker: str, opened: float, now: float) -> dict[str, Any] | None:
    """SHADOW (2026-11-27, user: "Using the price is probably better than the spot value for these tight turns. You'll see the price change before the spot value on perps"): the market's own price curve, the YES mid on a 1 s grid since the open, read with the wave lane's zig-zag in the PRICE's own typical minute (its root-mean-square 1 s move times sqrt(60), CLAUDE.md rule 1) and only a 5 s median of smoothing, against the spot reading's 30 s. Named values, never judged: pr_dir (+1 the YES price is rising), pr_swing_u, pr_gb_u (given back from its extreme), pr_age (seconds since the extreme), pr_u (the price's typical minute), pr_s (seconds of quotes). A lead-lag test on 24 markets (2026-11-27 15:30-16:15 ET) found the price leads the spot by about half a second to a second, so the gain to expect is the smoothing's, not the feed's; this reading is there to be scored on the lane's bets. None when there are not enough quotes. Memory only."""
    from . import ticks as ticks_mod
    from .waveshape import broad

    key = (ticker, int(now))
    with _lock:
        if key in _prices:
            return _prices[key]
    out = _price_curve(ticks_mod, broad, ticker, opened, now)
    with _lock:
        if len(_prices) > 2000:
            _prices.clear()
        _prices[key] = out
    return out


def _price_curve(ticks_mod: Any, broad: Any, ticker: str, opened: float, now: float) -> dict[str, Any] | None:
    rows = [r for r in ticks_mod.mids.quotes(ticker) if opened <= r[0] <= now and r[1] > 0 and r[2] > 0]
    if len(rows) < 20 or now - opened < PRICE_MIN_S:
        return None
    t = int(now - opened)
    g: list[float] = []
    k, last = 0, None
    for sec in range(0, t + 1):
        while k < len(rows) and rows[k][0] <= opened + sec:
            last = (rows[k][1] + rows[k][2]) / 2.0
            k += 1
        if last is None:
            if g:
                last = g[-1]
            else:
                continue
        g.append(last)
    if len(g) < PRICE_MIN_S:
        return None
    pu = (sum((g[i] - g[i - 1]) ** 2 for i in range(1, len(g))) / (len(g) - 1)) ** 0.5 * 60 ** 0.5
    if pu <= 0:
        return None
    sm = broad(g, 0, len(g) - 1, 2, len(g) - 1)
    pts, d, ek, ev = waveturn.zigzag(sm, PRICE_ZZ * pu)
    start = pts[-1][1] if pts else sm[0]
    if not d:
        return {"pr_dir": 0, "pr_swing_u": 0.0, "pr_gb_u": 0.0, "pr_age": 0, "pr_u": round(pu, 6), "pr_s": len(g)}
    return {"pr_dir": d, "pr_swing_u": round((ev - start) * d / pu, 3), "pr_gb_u": round((ev - sm[-1]) * d / pu, 3), "pr_age": len(g) - 1 - ek, "pr_u": round(pu, 6), "pr_s": len(g)}


def clear() -> None:
    with _lock:
        _reads.clear()
        _prices.clear()
        _starts.clear()


def facts(r: waveturn.Reading | None, h: waveturn.Hour, side: str, ok: bool, price: dict[str, Any] | None = None, shape: dict[str, float] | None = None) -> dict[str, Any]:
    """The reading as named values for the decision's record (buychain.Check.facts)."""
    out: dict[str, Any] = {"u": None if h.u is None else round(h.u, 8), "hour_leg_u": round(h.leg_u, 3), "hour_leg_s": round(h.leg_s, 1), "side": side, "buy": ok}
    if r is not None:
        out.update({"t": r.t, "dir": r.d, "swing_u": round(r.swing_u, 3), "gb_u": round(r.gb_u, 3), "raw_gb_u": round(r.raw_gb_u, 3), "pos_u": round(r.pos_u, 3), "swing_s": r.swing_s,
                    "leg_u": round(r.leg_u, 3), "size_x": round(r.size_x, 3), "pace_x": round(r.pace_x, 3), "jumpy": round(r.jumpy, 2)})
    if shape:
        out.update({"bow": shape["bow"], "flat": shape["flat"], "shape_size": shape["size"], "shape_secs": shape["secs"], "prev_ratio": shape.get("prev_ratio", 0.0)})
    if price:
        out.update(price)
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
    r, h, bad, shape = read(index, opened, now, K)
    try:
        pc = price_curve(shot.ticker, opened, now)
    except Exception:  # a shadow reading never stops a buy
        pc = None
    if r is None:
        return False, f"no low-side buy: {bad}", facts(None, h, shot.side, False, pc, shape)
    if h.leg_u <= 0:
        return False, f"no low-side buy: the hour before the open had no completed leg of {waveturn.DONE_U:g}u, not a wave market", facts(r, h, shot.side, False, pc, shape)
    want = -1 if shot.side == "yes" else 1  # YES is the low side in a dip (the spot falling), NO in a peak (rising)
    word = "dip" if shot.side == "yes" else "peak"
    move = "fall" if want == -1 else "rise"
    told = f"{r.t}s in: swing {r.swing_u:.2f}u {'up' if r.d == 1 else 'down' if r.d == -1 else '-'}, {r.gb_u:.2f}u given back, usual leg {h.leg_u:.2f}u, u {h.u:.6g} (target {K:.6g})"
    if r.d != want:
        return False, f"not in a {word} for {shot.side.upper()}: the swing is {'a rise' if r.d == 1 else 'a fall' if r.d == -1 else 'not there yet'}, not a {move}; {told}", facts(r, h, shot.side, False, pc, shape)
    if r.swing_u < DIP_U:
        return False, f"the {move} is {r.swing_u:.2f}u, under {DIP_U:g}u: not yet a {word}; {told}", facts(r, h, shot.side, False, pc, shape)
    if shape is None:
        return False, f"no half U: the {move}'s shape cannot be read yet; {told}", facts(r, h, shot.side, False, pc, shape)
    if shape.get("prev_ratio", 0.0) >= PREV_MAX:
        return False, f"not fading a recovery: the swing before this {move} was {shape['prev_ratio']:.2f} of its size (the most is {PREV_MAX:g}), so it is the way back from a bigger move, not a {word}; no bet; {told}", facts(r, h, shot.side, False, pc, shape)
    if shape["bow"] < HALF_U_BOW:
        return False, f"no half U: the {move} is straight (its path is {shape['bow']:.2f} of its size past the straight line, the least is {HALF_U_BOW:g}); {told}", facts(r, h, shot.side, False, pc, shape)
    if shape["flat"] > HALF_U_FLAT:
        return False, f"not at the middle of a half U: the {move} is still running at {shape['flat']:.2f} of its average pace over its last quarter (the most is {HALF_U_FLAT:g}); {told}", facts(r, h, shot.side, False, pc, shape)
    start = read_start(round(opened), now) + r.ext_t - r.swing_s  # the second this swing began
    if not resting and bet_times and any(b >= start - SWING_SLACK for b in bet_times):
        return False, f"swing_played: this lane already bought {shot.side.upper()} in this {move} (it began {int(now - start)}s ago); it waits for the next {word}; {told}", facts(r, h, shot.side, False, pc, shape)
    if not resting and restbuys.missed_since(shot.ticker, shot.side, start - SWING_SLACK):
        return False, f"swing_missed: a resting buy of {shot.side.upper()} in this {move} (it began {int(now - start)}s ago) rested its time and did not fill; the moment has passed, it waits for the next {word}; {told}", facts(r, h, shot.side, False, pc, shape)
    state = "its order stands" if resting else "buys"
    return True, f"in a {word} (a half U: bowed {shape['bow']:.2f}, pace {shape['flat']:.2f} of its average): the {move} has run {r.swing_u:.2f}u, {shot.side.upper()} is the low side, {state}; {told}", facts(r, h, shot.side, True, pc, shape)
