"""One risk number per side: Kalshi's price moved by the curve's readings.

2026-11-16, user: "we built all these logical stops and profit selling points, and they were supposed to contribute to a risk score for both buying and selling"; "nothing is absolute. all these factors need to build towards that risk score"; "we don't give a shit about a 55% risk"; "it should never be desirable to pick a 50/50"; "I thought you were putting our new logic up as a lane".

PAPER. The chance YES pays = logistic(sum of weight x reading), no constant, so a market's two sides add to 1. The readings are the ones scripts/risk_score_fit.py fits, built the same way, each from ticks stamped at or before the moment:
  price        the logit of Kalshi's YES mid
  cushion      the index against the target in the index's typical moves for the time left
  flip rate    changes of favorite so far per minute LAPSED (capped at 1.5), times the price's logit: how far the price is trusted
  time         the share of the market gone, times the price's logit
  t15 t30 t60  the target's place in the index's range over the 15, 30 and 60 minutes before the open, less a half
  margin       how far the series' last four markets finished from their targets, in typical moves (average)
  flips        how often the favorite changed in those four (average, less 2)
  nudge        the mid's move over the last minute in the market's own typical minutes
  trend3/15    the index's travel over the last 3 and 15 minutes, in its typical moves for that long
  trend5 x3    the 5-minute travel, counted in the cushion zone the market is in (on the line / fair / wide)
Weights: the walk-forward fit of 2026-11-16 20:30 (studies/results/2026-11-16-risk-score-fit.txt), replaced by data/risk_score.json when that file exists. On held-out days this number equalled Kalshi's mid (log-loss 0.5028 against 0.5024); it has NOT been shown to beat the price. What the lane tests is acting only on a risk that is far from even and has stayed there.
A reading outside what the fit saw is clipped to the fit's own limits. No coin, no price band, no minute band.
"""
from __future__ import annotations

import bisect
import json
import math
import time
from pathlib import Path
from typing import Any, Sequence

NAMES = ("price", "cushion", "flip rate", "time", "t15", "t30", "t60", "margin", "flips", "nudge", "trend3", "trend15", "trend5 near", "trend5 mid", "trend5 far")
WEIGHTS = (0.664, 0.461, 0.345, 0.068, -0.192, -0.207, 0.011, 0.201, 0.033, 0.027, -0.031, -0.155, 0.048, 0.074, 0.156)
STEP = 5.0  # seconds: the grid the market's mids are read on, as in the fit
HELD_LOOKS = (0.0, 30.0, 60.0)  # the number has to hold at each of these many seconds back: now, half a minute ago, a minute ago

_cache: dict[str, Any] = {"data": None, "mtime": -1.0, "checked": -1e9, "dir": None}
_prev: dict[tuple[str, int], list[tuple[float | None, int | None]]] = {}


def tuned(data_dir: str | None) -> dict[str, Any] | None:
    """data/risk_score.json as risktune.py last wrote it, from memory; the file is looked at no more than once every 30 s. None when there is none."""
    if not data_dir:
        return None
    now = time.monotonic()
    if now - _cache["checked"] >= 30.0 or _cache["dir"] != data_dir:
        _cache["checked"], _cache["dir"] = now, data_dir
        p = Path(data_dir) / "risk_score.json"
        try:
            m = p.stat().st_mtime
            if m != _cache["mtime"]:
                _cache["data"], _cache["mtime"] = json.loads(p.read_text()), m
        except (OSError, ValueError):
            _cache["data"], _cache["mtime"] = None, -1.0
    return _cache["data"]


def weights(data_dir: str | None = None) -> tuple[float, ...]:
    """The weights in use: the tuned ones (risktune.py) when the file has a complete set, else the 2026-11-16 fit."""
    got = (tuned(data_dir) or {}).get("weights")
    try:
        return tuple(float(got[n]) for n in NAMES) if got else WEIGHTS
    except (KeyError, TypeError, ValueError):
        return WEIGHTS


def tuned_gap(data_dir: str | None) -> tuple[float, float] | None | bool:
    """The gap range the record says to buy: (least, most); None when the record was tuned and no gap paid; False when there is no tune to read (the lane's own settings then stand)."""
    got = tuned(data_dir)
    if not got or "gap" not in got:
        return False
    g = got["gap"]
    return (float(g[0]), float(g[1])) if g else None


def _at(ts: Sequence[float], vs: Sequence[float], t: float) -> float | None:
    i = bisect.bisect_right(ts, t) - 1
    return vs[i] if i >= 0 and vs[i] > 0 else None


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def unit_before(ts: Sequence[float], vs: Sequence[float], opened: float) -> float | None:
    """The index's typical one-minute move over the hour before the open (root-mean-square of the 60 one-minute log moves). None without a tick at or before every minute mark, or a flat hour."""
    marks = [_at(ts, vs, opened - 60.0 * k) for k in range(60, -1, -1)]
    if any(m is None for m in marks):
        return None
    unit = math.sqrt(sum(math.log(marks[k + 1] / marks[k]) ** 2 for k in range(60)) / 60.0)
    return unit if unit > 0 else None


def grid(mids: Sequence[tuple[float, float]], opened: float, now: float) -> list[tuple[float, float]]:
    """The market's YES mid every 5 s from the open to `now`: the last quote at or before each mark; marks before the first quote are left out."""
    qt = [m[0] for m in mids]
    out = []
    t = float(int(opened))
    while t <= now:
        i = bisect.bisect_right(qt, t) - 1
        if i >= 0:
            out.append((t, mids[i][1]))
        t += STEP
    return out


def features(ts: Sequence[float], vs: Sequence[float], now: float, opened: float, strike: float, mids: Sequence[tuple[float, float]], prev: Sequence[tuple[float | None, int | None]]) -> tuple[float, ...] | None:
    """The readings for the YES side at `now`, in NAMES' order. `ts`, `vs`: the settlement index; `mids`: this market's (time, YES mid) rows; `prev`: the series' last four markets as (margin, flips), either None when unread. None when the market is under a minute old, its mid is at an edge (0.03 or under, 0.97 or over), it has had under six marks or no range, the index is stale (over 30 s) or its hour before the open cannot be read."""
    left = opened + 900.0 - now  # 900: a 15-minute market's length, structural
    unit = unit_before(ts, vs, opened)
    g = grid([m for m in mids if m[0] <= now], opened, now)
    if unit is None or strike <= 0 or left <= 0 or now - opened < 60.0 or len(g) < 7:
        return None
    mid = g[-1][1]
    k = bisect.bisect_right(ts, now) - 1
    if not (0.03 < mid < 0.97) or k < 0 or now - ts[k] > 30.0 or vs[k] <= 0 or max(m for _, m in g) <= min(m for _, m in g):
        return None
    spot = vs[k]
    lg = math.log(mid / (1.0 - mid))
    fav, nflips, sq, nsq = None, 0, 0.0, 0
    for (_, a), (_, b) in zip(g, g[1:]):
        sq += (b - a) ** 2
        nsq += 1
    for _, m in g:
        lead = "yes" if m >= 0.55 else "no" if m <= 0.45 else None
        if lead and fav and lead != fav:
            nflips += 1
        fav = lead or fav
    cush = math.log(spot / strike) / (unit * math.sqrt(left / 60.0))
    places = []
    j0 = bisect.bisect_right(ts, opened)
    for m in (15, 30, 60):
        seg = vs[bisect.bisect_right(ts, opened - 60.0 * m):j0]
        places.append((strike - min(seg)) / (max(seg) - min(seg)) - 0.5 if seg and max(seg) > min(seg) else 0.0)
    mg = [p[0] for p in prev if p[0] is not None]
    fl = [p[1] for p in prev if p[1] is not None]
    pm = sum(mg) / len(mg) if mg else 0.0
    pf = sum(fl) / len(fl) - 2.0 if fl else 0.0
    back = next((m for t, m in reversed(g) if t <= g[-1][0] - 60.0), mid)
    nudge = (mid - back) / (math.sqrt(sq / nsq) * math.sqrt(60.0 / STEP)) if nsq and sq > 0 else 0.0

    def travel(seconds: float) -> float:
        was = _at(ts, vs, now - seconds)
        return math.log(spot / was) / (unit * math.sqrt(seconds / 60.0)) if was else 0.0

    tr5 = _clip(travel(300.0), -4.0, 4.0)
    zone = 0 if abs(cush) < 0.5 else 1 if abs(cush) < 1.5 else 2
    return (lg, _clip(cush, -6.0, 6.0), min(1.5, nflips / max((now - opened) / 60.0, 1.0)) * lg, (now - opened) / 900.0 * lg, places[0], places[1], places[2], _clip(pm, -3.0, 3.0), _clip(pf, -2.0, 4.0), _clip(nudge, -3.0, 3.0),
            _clip(travel(180.0), -4.0, 4.0), _clip(travel(900.0), -4.0, 4.0), tr5 if zone == 0 else 0.0, tr5 if zone == 1 else 0.0, tr5 if zone == 2 else 0.0)


def chance(x: Sequence[float], side: str, w: Sequence[float] = WEIGHTS) -> float:
    """The chance `side` pays from the YES readings `x`."""
    z = max(-30.0, min(30.0, sum(a * b for a, b in zip(w, x))))
    p = 1.0 / (1.0 + math.exp(-z))
    return p if side == "yes" else 1.0 - p


def held(ts: Sequence[float], vs: Sequence[float], now: float, opened: float, strike: float, mids: Sequence[tuple[float, float]], prev: Sequence[tuple[float | None, int | None]], side: str, w: Sequence[float] = WEIGHTS) -> list[float] | None:
    """The side's chance now, 30 s ago and 60 s ago (HELD_LOOKS), each read only from ticks at or before its own moment. None when any of the three cannot be read: a number that cannot be shown to have held has not held."""
    out = []
    for back in HELD_LOOKS:
        x = features(ts, vs, now - back, opened, strike, mids, prev)
        if x is None:
            return None
        out.append(chance(x, side, w))
    return out


def low_risk(chances: Sequence[float] | None, max_risk: float) -> bool:
    """True when the side's risk (1 - chance) is at or under `max_risk` at every look: low, and it has stayed low."""
    return bool(chances) and all(1.0 - c <= max_risk for c in chances)


def gap_held(chances: Sequence[float] | None, kalshi: Sequence[float] | None, floor: float, ceiling: float) -> bool:
    """True when the score's chance has been over Kalshi's price for the side by at least `floor` and at most `ceiling` at every look (2026-11-16, user, on the level rule buying only 0.87 to 0.98 sides: "Why such high prices?"; "Ok let's try your suggestion"). The floor is what the buy costs; the ceiling is where the fit stopped being right (a disagreement of 3 to 6 points was the score's to win, one over 6 was Kalshi's: studies/results/2026-11-16-risk-score-fit.txt)."""
    if not chances or not kalshi or len(chances) != len(kalshi):
        return False
    if kalshi[0] < kalshi[-1]:
        return False  # the side's price is lower than at the oldest look: the gap is the price falling away from a number that follows it more slowly, not the number rating the side higher (2026-11-16 23:21: the lane's first four buys were sides in the middle of a fall; ZEC bought at 0.23 was 0.12 two minutes later)
    return all(floor <= c - k <= ceiling for c, k in zip(chances, kalshi))


SALE_SHARE = 0.5  # a side bought under twice the lane's line is sold at half the price paid instead: structural (it keeps the buy and the sale from contradicting each other), not a tuned number


def sale_line(line: float, entry: float) -> float:
    """The chance at or under which a loss is sold (lane field curve_risk_score): the lane's line, or half the price paid when that is lower. A side bought at 0.80 is sold at the lane's 0.25 (a risk of 75% that has stayed); a side bought at 0.24 would sit on that line from its first second (2026-11-16 23:23: ZEC bought at 0.23 was sold two minutes later), so it is sold at 0.12."""
    return min(line, SALE_SHARE * entry) if entry > 0 else line


def tag(chances: Sequence[float] | None, kalshi: float | None) -> str:
    if not chances:
        return "[risk score unreadable]"
    return f"[risk score: risk {100 * (1 - chances[0]):.0f}% now, {100 * (1 - chances[1]):.0f}% 30 s ago, {100 * (1 - chances[2]):.0f}% a minute ago | by Kalshi's price {'n/a' if kalshi is None else format(100 * (1 - kalshi), '.0f') + '%'}]"


def previous(ts: Sequence[float], vs: Sequence[float], series_mids: Sequence[tuple[float, float]], opened: float, unit: float | None) -> list[tuple[float | None, int | None]]:
    """The series' last four markets as (margin, flips), newest first, from memory alone (2026-11-16, user: "Why is it reading the db for anything? We are supposed to be reading from the memory tape"). A 15-minute series' markets run back to back, so the four before this one closed at `opened`, 15, 30 and 45 minutes earlier. Margin: the index's 60 s average before that close against that market's target (its 60 s average before its own open), in typical moves over 15 minutes. Flips: changes of favorite along that market's mids (`series_mids`: the series' (time, YES mid) rows of the last hour, stitched). Either is None when memory does not hold it."""
    from . import scenario

    out: list[tuple[float | None, int | None]] = []
    for k in range(4):
        close = opened - 900.0 * k  # 900: a 15-minute market's length, structural
        strike = scenario.target(ts, vs, close - 900.0)
        mg = scenario.margin(ts, vs, close, strike, unit) if strike and unit else None
        rows = [m for t, m in series_mids if close - 900.0 < t <= close]
        out.append((mg, scenario.flips(rows) if len(rows) >= 50 else None))
    return out


def gather(series: str, ticker: str, opened: float, now: float) -> tuple[list[float], list[float], list[tuple[float, float]], list[tuple[float | None, int | None]], float | None] | None:
    """What a live reading needs, all from memory: the settlement index's ticks from just over an hour and three quarters before the open (the last four markets and the hour before the oldest of them need them), this market's mids, the series' last four markets as (margin, flips), and the index's unit. None when the index's history does not reach an hour before the open."""
    from . import variants
    from . import ticks as ticks_mod

    ix = variants.spot_index(series, "", now)
    got = (ticks_mod.history.window(ix, opened - 6400.0, now) or ticks_mod.history.window(ix, opened - 3700.0, now)) if ix else None
    if not got:
        return None
    ts, vs = got
    unit = unit_before(ts, vs, opened)
    key = (series, int(opened))
    if key not in _prev:
        if len(_prev) > 400:
            _prev.clear()
        _prev[key] = previous(ts, vs, ticks_mod.mids.series_rows(series, opened, 3600.0), opened, unit)  # the four finished markets do not change while this one runs: read once per market
    return ts, vs, ticks_mod.mids.rows(ticker), _prev[key], unit


def live(series: str, ticker: str, side: str, close_ts: float, now: float, data_dir: str | None = None, looks: Sequence[float] = HELD_LOOKS) -> tuple[list[float], float, list[float], tuple[float, ...]] | None:
    """(the side's chance at each of `looks` seconds back, Kalshi's mid for the side now, Kalshi's mid for the side at each look) for a running market, or None when any look cannot be read. The target is the index's average over the 60 s before the open (scenario.target: Kalshi's own rule)."""
    from . import scenario

    opened = close_ts - 900.0
    got = gather(series, ticker, opened, now)
    if got is None:
        return None
    ts, vs, mids, prev, _ = got
    strike = scenario.target(ts, vs, opened)
    if strike is None or not mids:
        return None
    w = weights(data_dir)
    out, was, first = [], [], None
    for back in looks:
        x = features(ts, vs, now - back, opened, strike, mids, prev)
        if x is None:
            return None
        first = first or x
        out.append(chance(x, side, w))
        p_yes = 1.0 / (1.0 + math.exp(-x[0]))  # the first reading is the logit of Kalshi's YES mid at that look
        was.append(p_yes if side == "yes" else 1.0 - p_yes)
    return out, was[0], was, first  # `first`: the YES readings now, stored with the decision for the tune
