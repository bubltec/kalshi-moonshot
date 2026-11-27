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
import itertools
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
    phi = sum(a * b for a, b in itertools.pairwise(xc)) / den
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

    key = (index, round(opened))
    with _lock:
        if key in _before:
            return _before[key]
    got = ticks.history.window(index, opened - HOUR - GRID, opened - 1e-6)
    out: dict[str, Any] | None = None
    if got and got[0]:
        g = _grid(got[0], got[1], opened - HOUR, opened - GRID)
        if len(g) >= 300:
            d = [b - a for a, b in itertools.pairwise(g)]
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
    d10 = [b - a for a, b in itertools.pairwise(w)]
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

    key = (index, round(opened))
    with _lock:
        if key in _big:
            return _big[key]
    got = ticks.history.window(index, opened - HOUR - GRID, opened - 1e-6)
    out: float | None = None
    if got and got[0]:
        pre = grid(got[0], got[1], int(opened) - HOUR, int(opened))
        if len(pre) >= HOUR - 5 and None not in pre:
            sm = broad([float(x) for x in pre],  # type: ignore[arg-type]
                 0, len(pre) - 1, waveturn.SMOOTH // 2, len(pre) - 1)
            pts, _d, _k, _v = waveturn.zigzag(sm, BIG_REVERSAL * u)
            durs = [b[0] - a[0] for a, b in itertools.pairwise(pts)]
            out = float(sorted(durs)[len(durs) // 2]) if len(durs) >= 2 else None
    with _lock:
        if len(_big) > 512:
            _big.clear()
        _big[key] = out
    return out


def leg(index: str, opened: float, now: float, target: float, side: str, left: float, back: float = 0.0) -> dict[str, Any] | None:
    """Where the bet's side stands in the curve's current LEG, by walking the spot back from now to the window's open with the wave lane's zig-zag (waveturn.reading: swings of 0.5 typical minutes on a 30 s median, the pre-open hour's typical minute as the unit; no crossings, no gate). (2026-11-25, user: "why can't it walk the curve back and identify it was inside a dip?")
    class  'inside' (the current swing runs in the side's winning direction, is at least 0.5 u and has given back under 0.2 u: not turned), 'turned' (that swing has given back 0.2 u or more), 'against' (the current swing runs against the side), 'none' (no swing of 0.5 u yet)
    swing_u, gb_u   the current swing's size and what it has given back, in typical minutes; r2 the 30 minute straight-line fit (a trend when at or over R2_CUT)
    age, travel     how long the current BIG leg (reversal of 2 typical minutes) has run against the market's usual big leg of the hour before the open, and how far the current leg has travelled against the market's usual 10 minute wave travel (both 1.0 = a usual one)
    would_refuse    ('against' and 'turned' ALWAYS, from ~18:15 ET; 'inside' only at its extreme). The earlier text, narrowed at the user's word: (2026-11-25, user: "this new curve logic is supposed to just stop us from betting in a dip or peak when its super likely that the wave pattern will either repeat or turn into a long leg") 'inside' or 'against' AND AT ITS EXTREME: the leg has already run a usual big leg's length (age >= 1) or already travelled a usual wave's distance (travel >= 1). A young leg part-way through, and a straight breakout still short of a usual wave (BTC 12:45 at 12:33: age 0.52, travel 0.63), pass.
    None when the pre-open hour, the path since the open or the typical minute is not in the tick memory."""
    from . import ticks, waveturn
    from .wavetop import grid

    h = waveturn._hour(index, round(opened))
    if h.u is None or now - opened < 30:
        return None
    start = opened - max(back, 0.0)  # `back`: seconds before the open the walk starts from (0 = the open, as the lane has read it since 2026-11-25); the typical minute and the usual leg stay the hour before the open's
    got = ticks.history.window(index, start, now)
    if not got or not got[0]:
        return None
    T, V = got
    if T[0] > start:
        prev = ticks.history.last_at_or_before(index, start)
        if prev is not None:
            T, V = [prev[0], *T], [prev[1], *V]
    win = grid(T, V, int(start), int(now) + 1)
    if len(win) < 31 or None in win:
        return None
    r = waveturn.reading(win, int(now - start), target, h.u)  # type: ignore[arg-type]
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

    sm = broad([float(x) for x in win], 0, int(now - start), waveturn.SMOOTH // 2, int(now - start))  # type: ignore[arg-type]
    big, _d, _k, _v = waveturn.zigzag(sm, BIG_REVERSAL * h.u)
    big_start = big[-1][0] if big else 0
    usual = _usual_big_leg(index, opened, h.u)
    age = ((now - start) - big_start) / usual if usual else None
    small, _d2, _k2, _v2 = waveturn.zigzag(sm, waveturn.ZZ * h.u)
    small_start = small[-1][0] if small else 0
    prev_u = abs(small[-1][1] - small[-2][1]) / h.u if len(small) >= 2 else 0.0  # the swing BEFORE the current one (it ran the other way): the size of the turn the current swing started from
    bef = before(index, opened)
    w10 = bef["w10"] if bef and bef.get("w10") else None
    travel = abs(float(win[int(now - start)]) - float(win[small_start])) / w10 if w10 else None  # type: ignore[arg-type]
    at_extreme = (age is not None and age >= EXTREME) or (travel is not None and travel >= EXTREME)
    refuse = cls in ("against", "turned") or (cls == "inside" and at_extreme)  # 2026-11-25 ~18:15 (user, on ZEC 18:15 NO bought at 18:07:11 at the bottom of a dip as it turned up, then a straight run through the strike: "It bought right in the dip. Why??! Why doesn't it wait??"): a bet whose side the current swing runs AGAINST, or whose leg has already TURNED against it, always waits; only a bet inside its own leg is bought, and not at that leg's extreme. 24 hours of bets: against 18 bets -$42.92 (44% lost), turned 16 bets -$0.17 (31% lost)
    return {"class": cls, "swing_u": round(r.swing_u, 2), "gb_u": round(r.gb_u, 2), "r2": r2, "age": None if age is None else round(age, 2), "travel": None if travel is None else round(travel, 2), "would_refuse": refuse, "usual_leg_s": usual, "w10": w10, "at_extreme": at_extreme, "prev_u": round(prev_u, 2), "spot": float(win[int(now - start)])}  # type: ignore[arg-type]


def travel_left(bef: dict[str, Any] | None, left: float) -> float | None:
    """How far the market's waves usually travel in `left` seconds, from the hour before the open (`before`): its 3, 10 and 15 minute travels joined by straight lines; beyond 15 minutes the 15 minute travel grown by the square root of the extra time (a random walk's reach), under 3 minutes the 3 minute travel shrunk by the square root. None when the hour is not read."""
    if not bef:
        return None
    w3, w10, w15 = bef.get("w3"), bef.get("w10"), bef.get("w15")
    if not (w3 and w10 and w15):
        return None
    m = max(left, 1.0) / 60.0
    if m <= 3.0:
        return float(w3) * math.sqrt(m / 3.0)
    if m <= 10.0:
        return float(w3) + (float(w10) - float(w3)) * (m - 3.0) / 7.0
    if m <= 15.0:
        return float(w10) + (float(w15) - float(w10)) * (m - 10.0) / 5.0
    return float(w15) * math.sqrt(m / 15.0)


NEST_LEVELS = (1.0, 0.5, 0.25, 0.125)  # the swing sizes of `nest`, as shares of the market's own biggest swing
PEAK_SHARE = 0.1  # a bet is bought AT A PEAK when the swing it rides has pulled back less than this share of its own size from its extreme
_nest: dict[tuple[str, int, int], tuple[float, tuple[int, ...], tuple[tuple[int, float], ...]] | None] = {}


def price_swings(rows: list[tuple[float, float]], opened: float, now: float, side: str) -> dict[str, Any] | None:
    """The side's OWN PRICE swings since the market opened, cut by size from the market's own biggest price swing (2026-11-27, user, on the XRP 06:30 YES re-buy bought 16 s after the lane sold the same side at the top of its climb: "The peak reading looks at the spot's swings ... This is a bug we need to fix"; "I'd rather have that than a wait. The wait is a patch fix. This is a real fix. Ideally it shouldn't need a wait. The wait should just happen"). `rows` are the market's (time, YES mid) from the tick memory; the bet's side price is the mid (YES) or 1 - mid (NO), on a 10 s grid with the last quote carried. BIGGEST is the largest reversal at which that path still shows two swings; the swings are read at a quarter and an eighth of it, as `nest` reads the spot's. rising = the price's current swing runs UP at both sizes: it is still climbing, nothing has been given back at the small scale, so a buy now is a buy at the price's own peak. No length, no number of mine. On 312 live bets of 11-25/26 in curve markets (scripts/priskp_price_swings_table.py): buys with the price rising at both sizes 154, settled their way 68%, -$126.16 held, -$91.96 as run; buys after the price had pulled back at one of them 73, 71%, +$75.90 held. The wait it asks of a lane that has just sold at the top is not a timer: the price is still at its high. None with under two minutes of quotes or a path that never turned."""
    if side not in ("yes", "no"):
        return None
    pts = [(t, (m if side == "yes" else 1.0 - m)) for t, m in rows if opened <= t <= now]
    if len(pts) < 12:
        return None
    ts = [t for t, _ in pts]
    g: list[float] = []
    t = opened
    while t <= now:
        k = bisect.bisect_right(ts, t) - 1
        if k >= 0:
            g.append(pts[k][1])
        t += GRID
    if len(g) < 12:
        return None
    lo, hi = 0.0, max(g) - min(g)
    if hi <= 0:
        return None
    for _ in range(30):  # the largest reversal that still shows two swings
        mid = (lo + hi) / 2.0
        if _swing_dir(g, mid)[0] >= 2:
            lo = mid
        else:
            hi = mid
    if lo <= 0:
        return None
    dirs = [_swing_state(g, lo * s)[0] for s in (0.25, 0.125)]
    return {"dirs": dirs, "rising": all(d > 0 for d in dirs), "biggest": round(lo, 4)}


def _swing_state(g: list[float], rev: float) -> tuple[int, float]:
    """(the current direction, how far the path has pulled back from the current swing's extreme as a share of that swing's size) of a zig-zag of reversal `rev`; (0, 0.0) before the first swing."""
    d, lo, hi, ev, start = 0, g[0], g[0], g[0], g[0]
    for v in g[1:]:
        if d == 0:
            lo, hi = min(lo, v), max(hi, v)
            if v - lo >= rev:
                d, start, ev = 1, lo, v
            elif hi - v >= rev:
                d, start, ev = -1, hi, v
        elif (v - ev) * d > 0:
            ev = v
        elif (ev - v) * d >= rev:
            d, start, ev = -d, ev, v
    size = abs(ev - start)
    return d, (abs(g[-1] - ev) / size if d and size > 0 else 0.0)


def _swing_dir(g: list[float], rev: float) -> tuple[int, int]:
    """(turns confirmed, the current direction +1 / -1 / 0) of a zig-zag of reversal `rev` over the path."""
    d, lo, hi, ev, turns = 0, g[0], g[0], g[0], 0
    for v in g[1:]:
        if d == 0:
            lo, hi = min(lo, v), max(hi, v)
            if v - lo >= rev:
                d, ev, turns = 1, v, turns + 1
            elif hi - v >= rev:
                d, ev, turns = -1, v, turns + 1
        elif (v - ev) * d > 0:
            ev = v
        elif (ev - v) * d >= rev:
            d, ev, turns = -d, v, turns + 1
    return turns, d


def nest(index: str, opened: float, now: float, side: str) -> dict[str, Any] | None:
    """How the market's OWN swings nest for the bet's side now, with no wave length anywhere (2026-11-26, user: "Don't build on waves that are specific lengths. There could by multiple waves in 3 mins, or one wave in 15"; "Why doesn't it read before the markets open? It buys starting at 3 mins how would it know if it's not inside a curve?"; on putting this reading on the buy note first: "Ok I like that"). `leg` walks back only to the open with a swing of half a typical minute, so at minute 3 it reads a ripple as a swing and cannot see the move the market opened inside (BTC, ZEC and DOGE 22:30 NO, 2026-11-26: a one-typical-minute dip bought against a five-typical-minute climb into the open).
    The path is everything the lane can see: the hour before the open and the market so far, on the 10 s grid. It is cut into swings by SIZE: BIGGEST is the largest reversal at which the path still shows a turn (the biggest wave this market has made in view), and the levels are NEST_LEVELS shares of it. A swing takes whatever time it took.
    levels   the bet's side WITH (+1) or AGAINST (-1) the current swing at each level, biggest first
    signs    the same as text, e.g. "- + + +"
    with     how many levels run with the bet
    biggest_u  the biggest swing in the market's typical minutes (the hour before the open), None when that is unread
    peak, at_peak  (2026-11-27, user, on ETH 00:45 YES bought at the top of a bounce: "It also bought on a peak which it should never do for a favorite") at the quarter and at the eighth of the biggest swing, whether the swing runs the bet's way and has pulled back less than PEAK_SHARE of its own size from its extreme; at_peak when both say so. On 210 live bets (scripts/priskp_pivot_trend_table.py) a buy at a peak made +$0.12 to +$0.38 a bet held and lost as run at both sizes, a buy off the peak +$1.25 to +$1.43 a bet; the trend of the swing highs and lows did not separate the bets
    A FACT, deciding nothing: studies/results/2026-11-26_priskp_nested_swings_at_buy_table.txt (210 live bets: 3 of 4 with settled the bet's way 84% and was positive on both days; 2 of 4, the bounce against the larger move, 62%, and 4 of 4, the move fully run, 71%, both negative on both days; a day and a half, a lead). None when the hour before the open is not in the tick memory or the path never turned. Memory only; the swings are kept per index for 5 s (both sides and every look in that time share one cut)."""
    from . import ticks

    key = (index, round(opened), int(now // 5))
    with _lock:
        hit = _nest.get(key, False)
    if hit is False:
        hit = None
        got = ticks.history.window(index, opened - HOUR - GRID, now)
        if got and got[0]:
            g = _grid(got[0], got[1], opened - HOUR, now)
            if len(g) >= 300:
                lo, hi = 0.0, max(g) - min(g)
                for _ in range(30):  # the largest reversal that still shows two swings
                    mid = (lo + hi) / 2.0
                    if _swing_dir(g, mid)[0] >= 2:
                        lo = mid
                    else:
                        hi = mid
                if lo > 0:
                    dirs = tuple(_swing_dir(g, lo * s * 0.999)[1] for s in NEST_LEVELS)
                    if 0 not in dirs:
                        hit = (lo, dirs, tuple(_swing_state(g, lo * s) for s in (0.25, 0.125)))
        with _lock:
            if len(_nest) > 2048:
                _nest.clear()
            _nest[key] = hit
    if hit is None or side not in ("yes", "no"):
        return None
    big, dirs, states = hit
    want = 1 if side == "yes" else -1
    levels = [d * want for d in dirs]
    peak = [d == want and back <= PEAK_SHARE for d, back in states]  # at the quarter and at the eighth of the biggest swing: the swing runs the bet's way and has not pulled back
    bef = before(index, opened)
    u = bef["u"] if bef and bef.get("u") else None
    return {"levels": levels, "signs": " ".join("+" if v > 0 else "-" for v in levels), "with": sum(v > 0 for v in levels), "biggest_u": None if not u else round(big / u, 1), "peak": peak, "at_peak": all(peak)}


TREND_MIN = 1.0  # a swing counts as a TREND once it has run this many typical minutes with the bet and has not turned: my choice, twice wave_turn's 0.5 u swing, not scored
REACH = 1.0  # the target is IN REACH of the curve when the spot is within this many of the market's usual 10 minute wave travels of it (the hour before the open): outside that the market is flat with cushion


def curve(index: str, opened: float, now: float, target: float, side: str, left: float, back: float = 0.0) -> dict[str, Any] | None:
    """The curve verdict for one side (2026-11-25 ~18:30, user, after ZEC 18:15 NO was bought at the bottom of a dip as it turned up and the lane then sat out the whole rise: "ONLY in the curve do you buy on a trend. If it's flat with cushion or a trend market you buy whenever price is good"; "I'm asked you to wait in the dip or peaks. You need to buy on a trend").
    regime  'curve' when the market has waves (at least two confirmed big legs in the hour before the open) AND the target is within REACH usual wave travels of the spot; else 'steady' (flat with cushion, or one trending line): the lane's own checks decide there, whenever the price is good
    verdict (curve only) 'buy' when the bet's side runs WITH the current swing ('inside'), the swing has run TREND_MIN typical minutes without turning, and started from a turn (the swing before it, the other way, ran TREND_MIN too), and the leg is not at its extreme: the trend entry, which replaces the five rank checks; otherwise 'wait' (at a dip or a peak: the swing is against the bet, or it has turned, or it is young, or at its extreme)
    None when the leg cannot be read (the hour before the open or the path since it is not in memory): the lane's checks decide."""
    base = leg(index, opened, now, target, side, left, back)
    if base is None:
        return None
    usual = base.get("usual_leg_s")
    bef = before(index, opened)
    room = travel_left(bef, left)
    if not room or room <= 0:
        return None
    reach = abs(base["spot"] - target) / room  # the target's distance in the market's usual wave travel over the TIME LEFT, not over a fixed 10 minutes (2026-11-26, user, on HYPE 21:00 NO bought at minute 2.1 with 12.9 minutes left: "if you're betting on a curve early you need to be calculating for additional swings and variance possibilities based on the recent history"): early in the market there is time for a longer swing than a 10 minute one
    wavy = usual is not None and reach < REACH
    out = {**base, "reach": round(reach, 2), "regime": "curve" if wavy else "steady", "verdict": "steady"}
    if wavy:
        trend = base["class"] == "inside" and base["swing_u"] >= TREND_MIN and base["prev_u"] >= TREND_MIN and not base["at_extreme"]  # 2026-11-25 ~19:30: the swing must have STARTED FROM A TURN (the swing before it, the other way, was a real one), not be a fall or rise continuing into its low or high (BTC 19:05 NO: a 1.05 u fall bought mid-dip, -$13; user: "wait in the dip", "buy on a trend")
        out["verdict"] = "buy" if trend else "wait"
    return out
