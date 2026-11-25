"""Wave readings for the facts board (2026-11-25; user, after the 08:30 round, on what the lane should know about a market's waves: "maybe like a quick test at the start of each market that evaluates 'waves before'", "and 'current waves'", "those can go on the fact board for all steps to use"; "gaussian curve math"; "the range on the curve needs to be flexible because even though the shapes are similar, the distance is not"; "some are stretched waves that traverse 10 minutes, others are 3 minutes").

Two readings, both from the spot's tick memory (Kalshi's own index, ticks.history), both in the market's OWN units so a wave of any size or length reads the same (CLAUDE.md rule 1). Stdlib only, memory only; nothing here decides a buy or a sale, it only tells.

WAVES BEFORE (`before`, once per market, kept): from the hour before the open on a 10 s grid.
  u       the spot's typical minute (the root-mean-square 10 s change x sqrt(6))
  tau_s   the wave's own time scale: -10 / ln(lag-1 autocorrelation) of the last 30 minutes before the open, the decay time of a mean-reverting Gaussian (an Ornstein-Uhlenbeck fit); 3 minutes for a quick wave, 10 for a stretched one; None when no reversion shows
  w3, w10, w15   the root-mean-square change of the spot over 3, 10 and 15 minutes: how far the waves of each size usually travel in this market
  sd30    the standard deviation of the last 30 minutes before the open: the market's wave width

WAVES NOW (`now_read`, on every check): from the last 30 minutes on the same grid, signed for the bet's side (positive = toward the side's win).
  z         the spot against its own 30 minute mean, in its standard deviation, signed
  r2        how well a straight line fits those 30 minutes (high = a trend, low = a wave)
  slope     that line's slope, in standard deviations per minute, signed
  run5      how far the side has run in the last 5 minutes from the spot's extreme against it, in typical 10 s moves
  pull5     how far the spot has come back from the side's best in the last 5 minutes, in typical 10 s moves
  minute    the last minute's move, in the spot's typical minute, signed
  widths    the target's distance from the spot on the side's winning side, in wave widths (30 minute standard deviation); small = the target is inside the wave
  tau_s     the time scale as above, from the last 30 minutes
  chance    the Gaussian chance of ending on the side's winning side, mean-reverting (centre, width and tau of the last 30 minutes, the time left), or a random walk's when no reversion shows

Studies behind the choices: studies/results/2026-11-25-priskp-zscore-wave.txt (24 hours of priskp bets). They are a lead only; the readings are facts for steps to read, none is a rule.
"""

from __future__ import annotations

import bisect
import math
import statistics
import threading
from typing import Any

GRID = 10  # seconds between samples
HOUR, HALF = 3600, 1800
_lock = threading.Lock()
_before: dict[tuple[str, int], dict[str, Any] | None] = {}


def _grid(T: list[float], V: list[float], a: float, b: float) -> list[float]:
    """The path on a 10 s grid from a to b, the last tick carried forward."""
    out: list[float] = []
    t = a
    while t <= b + 1e-9:
        k = bisect.bisect_right(T, t) - 1
        if k >= 0:
            out.append(float(V[k]))
        t += GRID
    return out


def _tau(w: list[float]) -> float | None:
    mu = statistics.fmean(w)
    xc = [x - mu for x in w]
    den = sum(x * x for x in xc)
    if den <= 0:
        return None
    phi = sum(a * b for a, b in zip(xc, xc[1:])) / den
    return -GRID / math.log(phi) if 0.0 < phi < 0.9999 else None


def _rms_change(g: list[float], seconds: int) -> float | None:
    k = seconds // GRID
    if len(g) <= k + 5:
        return None
    d = [g[i + k] - g[i] for i in range(len(g) - k)]
    return math.sqrt(sum(x * x for x in d) / len(d))


def before(index: str, opened: float) -> dict[str, Any] | None:
    """The market's waves before its open (see the module doc), read once and kept; None when the hour before the open is not in the tick memory."""
    from . import ticks

    key = (index, int(round(opened)))
    with _lock:
        if key in _before:
            return _before[key]
    got = ticks.history.window(index, opened - HOUR - GRID, opened - 1e-6)
    out: dict[str, Any] | None = None
    if got and got[0]:
        g = _grid(got[0], got[1], opened - HOUR, opened - GRID)
        if len(g) >= 300:
            d = [b - a for a, b in zip(g, g[1:])]
            u = math.sqrt(sum(x * x for x in d) / len(d) * 6.0)
            half = g[-HALF // GRID:]
            tau = _tau(half)
            out = {
                "u": u,
                "tau_s": tau,
                "w3": _rms_change(g, 180),
                "w10": _rms_change(g, 600),
                "w15": _rms_change(g, 900),
                "sd30": statistics.pstdev(half),
            }
    with _lock:
        if len(_before) > 512:
            _before.clear()
        _before[key] = out
    return out


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def now_read(index: str, now: float, target: float, side: str, left: float) -> dict[str, Any] | None:
    """The waves now for one side against the market's target (see the module doc); None when the last 30 minutes are not in the tick memory or the spot did not move."""
    from . import ticks

    got = ticks.history.window(index, now - HALF - GRID, now)
    if not got or not got[0]:
        return None
    w = _grid(got[0], got[1], now - HALF, now)
    if len(w) < 120:
        return None
    n = len(w)
    mu, sd = statistics.fmean(w), statistics.pstdev(w)
    if sd <= 0:
        return None
    sign = 1.0 if side == "yes" else -1.0
    xm = (n - 1) / 2
    sxx = sum((i - xm) ** 2 for i in range(n))
    slope = sum((i - xm) * (y - mu) for i, y in enumerate(w)) / sxx
    icpt = mu - slope * xm
    res = sum((y - (icpt + slope * i)) ** 2 for i, y in enumerate(w))
    r2 = 1.0 - res / (sd * sd * n)
    d10 = [b - a for a, b in zip(w, w[1:])]
    u10 = math.sqrt(sum(x * x for x in d10) / len(d10)) or 1e-12
    five = w[-HALF // 6 // GRID:]  # the last 5 minutes (30 samples)
    x = w[-1]
    lo, hi = min(five), max(five)
    run, pull = ((x - lo), (hi - x)) if side == "yes" else ((hi - x), (x - lo))
    dist = sign * (x - target)
    tau = _tau(w)
    T = max(left, 1.0)
    if tau is None:
        chance = _phi(dist / (u10 * math.sqrt(6.0) * math.sqrt(T / 60.0))) if dist else 0.5
    else:
        mean_T = mu + (x - mu) * math.exp(-T / tau)
        sd_T = sd * math.sqrt(max(1.0 - math.exp(-2.0 * T / tau), 1e-9))
        chance = _phi((sign * (mean_T - target)) / sd_T)
    return {
        "z": round(sign * (x - mu) / sd, 3),
        "r2": round(r2, 3),
        "slope": round(sign * slope * 6.0 / sd, 4),
        "run5": round(run / u10, 2),
        "pull5": round(pull / u10, 2),
        "minute": round(sign * (x - w[-7]) / (u10 * math.sqrt(6.0)), 3),
        "widths": round(dist / sd, 3),
        "tau_s": None if tau is None else round(tau, 1),
        "chance": round(chance, 4),
    }


R2_CUT = 0.24  # kept for the note only since 2026-11-25 (the straight-line fit of the last 30 minutes; the verdict no longer reads it)
EXTREME = 1.0  # a leg is AT ITS EXTREME when it has already run a usual big leg's length or already travelled a usual wave's distance (the market's own, from the hour before the open); fixed before the study that chose it, studies/results/2026-11-25-priskp-zscore-wave.txt
BIG_REVERSAL = 2.0  # the big swings: a reversal of this many typical minutes

_big: dict[tuple[str, int], float | None] = {}


def _usual_big_leg(index: str, opened: float, u: float) -> float | None:
    """The median length in seconds of the market's confirmed BIG legs (zig-zag reversals of BIG_REVERSAL typical minutes on the 30 s median) over the hour before the open, kept per market; None with fewer than two."""
    from . import ticks, waveturn
    from .waveshape import broad
    from .wavetop import grid

    key = (index, int(round(opened)))
    with _lock:
        if key in _big:
            return _big[key]
    got = ticks.history.window(index, opened - HOUR - GRID, opened - 1e-6)
    out: float | None = None
    if got and got[0]:
        pre = grid(got[0], got[1], int(opened) - HOUR, int(opened))
        if len(pre) >= HOUR - 5 and None not in pre:
            sm = broad([float(x) for x in pre], 0, len(pre) - 1, waveturn.SMOOTH // 2, len(pre) - 1)
            pts, _d, _k, _v = waveturn.zigzag(sm, BIG_REVERSAL * u)
            durs = [b[0] - a[0] for a, b in zip(pts, pts[1:])]
            out = float(sorted(durs)[len(durs) // 2]) if len(durs) >= 2 else None
    with _lock:
        if len(_big) > 512:
            _big.clear()
        _big[key] = out
    return out


def leg(index: str, opened: float, now: float, target: float, side: str, left: float) -> dict[str, Any] | None:
    """Where the bet's side stands in the curve's current LEG, by walking the spot back from now to the window's open with the wave lane's zig-zag (waveturn.reading: swings of 0.5 typical minutes on a 30 s median, the pre-open hour's typical minute as the unit; no crossings, no gate). (2026-11-25, user: "why can't it walk the curve back and identify it was inside a dip?")
    class  'inside' (the current swing runs in the side's winning direction, is at least 0.5 u and has given back under 0.2 u: not turned), 'turned' (that swing has given back 0.2 u or more), 'against' (the current swing runs against the side), 'none' (no swing of 0.5 u yet)
    swing_u, gb_u   the current swing's size and what it has given back, in typical minutes; r2 the 30 minute straight-line fit (a trend when at or over R2_CUT)
    age, travel     how long the current BIG leg (reversal of 2 typical minutes) has run against the market's usual big leg of the hour before the open, and how far the current leg has travelled against the market's usual 10 minute wave travel (both 1.0 = a usual one)
    would_refuse    ('against' and 'turned' ALWAYS, from ~18:15 ET; 'inside' only at its extreme). The earlier text, narrowed at the user's word: (2026-11-25, user: "this new curve logic is supposed to just stop us from betting in a dip or peak when its super likely that the wave pattern will either repeat or turn into a long leg") 'inside' or 'against' AND AT ITS EXTREME: the leg has already run a usual big leg's length (age >= 1) or already travelled a usual wave's distance (travel >= 1). A young leg part-way through, and a straight breakout still short of a usual wave (BTC 12:45 at 12:33: age 0.52, travel 0.63), pass.
    None when the pre-open hour, the path since the open or the typical minute is not in the tick memory."""
    from . import ticks, waveturn
    from .wavetop import grid

    h = waveturn._hour(index, int(round(opened)))
    if h.u is None or now - opened < 30:
        return None
    got = ticks.history.window(index, opened, now)
    if not got or not got[0]:
        return None
    T, V = got
    if T[0] > opened:
        prev = ticks.history.last_at_or_before(index, opened)
        if prev is not None:
            T, V = [prev[0], *T], [prev[1], *V]
    win = grid(T, V, int(opened), int(now) + 1)
    if len(win) < 31 or None in win:
        return None
    r = waveturn.reading(win, int(now - opened), target, h.u)  # type: ignore[arg-type]
    want = -1 if side == "no" else 1
    if r.d == 0 or r.swing_u < waveturn.SWING:
        cls = "none"
    elif r.d == -want:
        cls = "against"
    elif r.gb_u < waveturn.GB:
        cls = "inside"
    else:
        cls = "turned"
    nowr = now_read(index, now, target, side, left)
    r2 = None if nowr is None else nowr["r2"]
    from .waveshape import broad

    sm = broad([float(x) for x in win], 0, int(now - opened), waveturn.SMOOTH // 2, int(now - opened))
    big, _d, _k, _v = waveturn.zigzag(sm, BIG_REVERSAL * h.u)
    big_start = big[-1][0] if big else 0
    usual = _usual_big_leg(index, opened, h.u)
    age = ((now - opened) - big_start) / usual if usual else None
    small, _d2, _k2, _v2 = waveturn.zigzag(sm, waveturn.ZZ * h.u)
    small_start = small[-1][0] if small else 0
    bef = before(index, opened)
    w10 = bef["w10"] if bef and bef.get("w10") else None
    travel = abs(float(win[int(now - opened)]) - float(win[small_start])) / w10 if w10 else None
    at_extreme = (age is not None and age >= EXTREME) or (travel is not None and travel >= EXTREME)
    refuse = cls in ("against", "turned") or (cls == "inside" and at_extreme)  # 2026-11-25 ~18:15 (user, on ZEC 18:15 NO bought at 18:07:11 at the bottom of a dip as it turned up, then a straight run through the strike: "It bought right in the dip. Why??! Why doesn't it wait??"): a bet whose side the current swing runs AGAINST, or whose leg has already TURNED against it, always waits; only a bet inside its own leg is bought, and not at that leg's extreme. 24 hours of bets: against 18 bets -$42.92 (44% lost), turned 16 bets -$0.17 (31% lost)
    return {"class": cls, "swing_u": round(r.swing_u, 2), "gb_u": round(r.gb_u, 2), "r2": r2, "age": None if age is None else round(age, 2), "travel": None if travel is None else round(travel, 2), "would_refuse": refuse}
