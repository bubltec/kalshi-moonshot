"""The longer trend behind a wave-top bet, as a template built by a background job and a shadow reading (2026-11-22).

User, on the wave-top lane's rising-hour rule: "it definitely depends on how much it is trending, and i think 1 hour isn't enough time for a pattern to emerge"; "3 hours is more likely to show a general shift"; "it shouldn't be a hard NO"; "since we're betting against the trend should go against as well"; "Ok let's try that" (a shadow nudge first).

Trend strength = the spot's change over H hours before the market's open, divided by the spot's typical minute (the root-mean-square 60-second change over the hour before the open) times sqrt(60 H): the drift against a random walk's own spread, in the market's own units, with no asset and no dollar figure. It is signed for the bet: positive = the trend runs the bet's way (a rise for a YES bet, a fall for a NO bet).

Replay (scripts/wave_avg_replay_cache.py, studies/results/2026-11-22-wave-avg-replay.txt, 1,226 crypto bets, 11-09 to 11-21): the worth of a bet a contract, hold to settlement, against the signed trend, by least squares: 24 h -0.0236 +- 0.0117, 12 h -0.0192 +- 0.0120, 6 h -0.0021, 3 h +0.0183 +- 0.0135, 1 h -0.0162 +- 0.0142. Bets against the 12 to 24 h trend made about +$1.2 to +$1.9 a bet and bets with it -$0.5 to -$0.7; at 3 h there was no difference. A lead from five horizons read after the fact; SHADOW ONLY (CLAUDE.md rule 5): the step `wave_trend` writes these readings and the nudge into the decision's note and changes nothing the lane buys.

Memory only on the tick path. A background job (`refresh`, hourly, beside marketprofile.refresh in the watcher's maintenance pass) reads the last 26 hours of each wave index from the tape, one sample a minute, into a small template; the readings come from that and from the tick memory.
"""

from __future__ import annotations

import bisect
import logging
import math
import threading
import time
from typing import Any

log = logging.getLogger("moonshot")

HORIZONS = (1.0, 3.0, 12.0, 24.0)
KEEP_HOURS = 26.0
NUDGE_PER_UNIT = -0.0236  # dollars a contract, per unit of trend signed for the bet, from the 24 h replay: against the trend (negative) the bet is worth more, with it less
NEAR = 900.0  # a template sample counts for a time up to this far after it
_GRID: dict[str, tuple[list[float], list[float]]] = {}
_lock = threading.Lock()


def clear() -> None:
    with _lock:
        _GRID.clear()


def refresh(s: Any, indexes: list[str], tape: Any, now: float | None = None) -> int:
    """Rebuild the template: for each index, the spot at every whole minute of the last KEEP_HOURS from the tape. A background job: it is the only place here that reads the database. Returns the number of samples held."""
    if tape is None:
        return 0
    now = time.time() if now is None else now
    total = 0
    for idx in sorted(set(indexes)):
        try:
            rows = tape.execute(
                "SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 AND CAST(ts AS BIGINT) % 60 = 0 ORDER BY ts",
                (idx, now - KEEP_HOURS * 3600.0),
            ).fetchall()
        except Exception:
            log.debug("wave trend: could not read %s from the tape", idx, exc_info=True)
            continue
        T = [float(r[0]) for r in rows]
        V = [float(r[1]) for r in rows]
        if T:
            with _lock:
                _GRID[idx] = (T, V)
            total += len(T)
    log.info("wave trend template: %d samples for %d indexes", total, len(_GRID))
    return total


def value_at(index: str, t: float) -> float | None:
    """The spot at time `t`: from the tick memory when it reaches back that far (a few hours), else from the template (a sample no more than NEAR seconds before `t`)."""
    from . import ticks as ticks_mod

    got = ticks_mod.history.window(index, t - 300.0, t)
    if got and got[1]:
        return float(got[1][-1])
    with _lock:
        g = _GRID.get(index)
    if not g:
        return None
    T, V = g
    j = bisect.bisect_right(T, t) - 1
    if j < 0 or t - T[j] > NEAR:
        return None
    return V[j]


def trends(index: str, opened: float, spot_open: float, unit: float) -> dict[float, float]:
    """The trend strength at each horizon that has a value (see the module's description); `unit` is the spot's typical minute before the open."""
    out: dict[float, float] = {}
    if unit <= 0:
        return out
    for h in HORIZONS:
        v = value_at(index, opened - h * 3600.0)
        if v is not None:
            out[h] = (spot_open - v) / (unit * math.sqrt(60.0 * h))
    return out


def for_bet(trend: float, side: str) -> float:
    """The trend signed for the bet: positive when it runs the bet's way."""
    return trend if side == "yes" else -trend


def nudge(z24_for_bet: float) -> float:
    """What the 24 h trend would move the bet's worth by, dollars a contract (the trend clipped to +-3 units, as in the replay)."""
    return NUDGE_PER_UNIT * max(-3.0, min(3.0, z24_for_bet))


NOW_SECONDS = (30, 60, 120, 300)  # the "right now" horizons: the last half minute, minute, two and five minutes
NOW_SMOOTH = 10  # the spot at each end of a horizon is the mean of its last this many seconds (one tick is noise)


def now_drift(sp: list[Any], unit: float, side: str) -> dict[int, float]:
    """The spot's drift over the last 30 s, 1, 2 and 5 minutes before the end of the grid `sp` (one value a second, the last is now), signed for the bet (positive = the spot is moving the bet's way), in the spot's typical minute: the change over a random walk's spread for that long, the same unit as the longer trends. A horizon whose history has a gap is left out. 2026-11-23 (user, for the scalping lane: "what matters is mostly right now", the trend in the last 5 minutes, the longer history only a weak nudge): written beside the long trends, nothing acts on it."""
    out: dict[int, float] = {}
    if unit <= 0 or len(sp) < max(NOW_SECONDS) + NOW_SMOOTH + 1:
        return out
    tail = sp[-(max(NOW_SECONDS) + NOW_SMOOTH + 1) :]
    if None in tail:
        return out
    now_v = sum(tail[-NOW_SMOOTH:]) / NOW_SMOOTH  # type: ignore[arg-type]
    for h in NOW_SECONDS:
        old = tail[len(tail) - 1 - h - NOW_SMOOTH + 1 : len(tail) - h]
        if len(old) < NOW_SMOOTH:
            continue
        z = (now_v - sum(old) / len(old)) / (unit * math.sqrt(h / 60.0))  # type: ignore[arg-type]
        out[h] = for_bet(z, side)
    return out


def now_text(sp: list[Any], unit: float, side: str) -> str:
    d = now_drift(sp, unit, side)
    if not d:
        return ""
    return "now for the bet (+ = the spot is moving its way, in typical minutes) " + ", ".join(f"{h} s {z:+.1f}" if h < 60 else f"{h // 60} m {z:+.1f}" for h, z in sorted(d.items())) + "; "


def note(index: str, opened: float, now: float, side: str) -> str:
    """The shadow sentence for a bet on `side`: the signed trend at each horizon and the nudge, from memory. Never raises."""
    from . import ticks as ticks_mod
    from . import wavetop

    try:
        a = int(opened) - 3800
        got = ticks_mod.history.window(index, float(a), now)
        if not got or not got[0]:
            return "trend: no tick history in memory (shadow)"
        sp = wavetop.grid(got[0], got[1], a, int(now) + 1)
        ia = int(opened) - a  # the open's index on the grid
        if len(sp) <= ia or None in sp[ia - 3600 : ia + 1]:
            return "trend: the spot's history does not cover the hour before the open (shadow)"
        unit = wavetop.typical_minute(sp, ia)  # type: ignore[arg-type]
        tr = trends(index, opened, sp[ia], unit)  # type: ignore[arg-type]
        if not tr:
            return "trend: no earlier spot value in memory yet (shadow)"
        parts = ", ".join(f"{h:g}h {for_bet(z, side):+.1f}" for h, z in sorted(tr.items()))
        z24 = tr.get(24.0)
        tail = f"; the 24 h nudge would move its worth by {100 * nudge(for_bet(z24, side)):+.1f}c a contract" if z24 is not None else "; no 24 h value yet, no nudge"
        return f"{now_text(sp, unit, side)}trend for the bet (+ = the trend runs its way) {parts}{tail} (shadow, nothing changes)"
    except Exception as exc:
        return f"trend unreadable ({exc!r}) (shadow)"
