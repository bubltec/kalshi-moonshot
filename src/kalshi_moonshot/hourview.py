"""The hour view of every active market, built in the BACKGROUND every five minutes and read from memory (2026-11-23; user: "the hour view should be computed every 5 mins in the background for any active market", then "we can use it as a tie breaker in situations where the risk isn't sure").

CLAUDE.md: nothing computed per tick and no database call in buy or sell logic; a reading that needs history is built by a background job into a per-market template and read from memory. This is that template. `maybe_refresh` (called from the watcher's background refit pass) rebuilds it at most every EVERY seconds from the tick memory (ticks.history), for every 15-minute market that is open or about to open and whose index has a strike; `read` answers from memory only, for the bet's side, and says nothing (None) when the view is older than twice EVERY.

The readings are the hour profile scripts/priskp_run.py recorded (same definitions, so the study and the live board agree): in the market's own typical minute u (the last 15 minutes' root-mean-square one-second log move per minute x sqrt(60)), for the 60 minutes before now:
  drift    the log move over the hour in the side's direction, in u (positive = the hour moved the bet's way)
  range    the hour's log high-to-low in u
  cross    how many times the index crossed the strike in the hour
  eff      the net log move over the sum of the one-second moves (how straight the hour was)
  hi_ago / lo_ago  minutes since the side's best and worst moment of the hour
It is a TIE-BREAKER, not a gate (user): the study (scripts/priskp_fact_risk_study.py, 4,756 buys, 2026-11-23) found no risk in the hour within a price band, so for now it is only logged, and it is flagged where it would speak: when the risk is not sure.
"""

from __future__ import annotations

import bisect
import itertools
import logging
import math
import threading
import time
from typing import Any

log = logging.getLogger("moonshot")

EVERY = 300.0  # seconds between two rebuilds (user: every five minutes)
HOUR = 3600.0
_lock = threading.Lock()
_VIEW: dict[str, dict[str, Any]] = {}
_built_at = 0.0


def build_one(T: list[float], V: list[float], now: float, strike: float) -> dict[str, Any] | None:
    """The side-neutral (YES-view) hour readings from one index's ticks at `now`, or None when the history is too thin (under 25 of the last 60 minutes, or under 5 of the last 15, of one-second ticks) or the index did not move."""
    if not T or strike <= 0:
        return None
    j = bisect.bisect_right(T, now) - 1
    i15, i60 = bisect.bisect_left(T, now - 900.0), bisect.bisect_left(T, now - HOUR)
    if j < 1 or j - i60 < 1500 or j - i15 < 300:
        return None
    sq = sum(math.log(V[k] / V[k - 1]) ** 2 for k in range(i15 + 1, j + 1) if V[k] > 0 and V[k - 1] > 0)
    if T[j] <= T[i15] or sq <= 0:
        return None
    u = math.sqrt(sq / (T[j] - T[i15]) * 60.0)
    g = [V[max(bisect.bisect_right(T, now - HOUR + 10.0 * x) - 1, 0)] for x in range(361)]
    if min(g) <= 0:
        return None
    mv = sum(abs(math.log(b / a)) for a, b in itertools.pairwise(g))
    hi, lo = max(range(361), key=lambda x: g[x]), min(range(361), key=lambda x: g[x])
    crosses = sum(1 for a, b in itertools.pairwise(g) if (a - strike) * (b - strike) < 0)
    return {
        "u": u,
        "drift_yes": math.log(g[-1] / g[0]) / u,
        "range": math.log(g[hi] / g[lo]) / u,
        "cross": float(crosses),
        "eff": abs(math.log(g[-1] / g[0])) / mv if mv > 0 else 0.0,
        "hi_yes_ago": (360 - hi) / 6.0,
        "lo_yes_ago": (360 - lo) / 6.0,
        "bins_yes": [round(math.log(max(sum(g[30 * b : 30 * b + 30]) / 30.0, 1e-12) / strike) / u, 3) for b in range(12)],  # twelve 5-minute averages against the strike, in u (the study's `bins`)
    }


def refresh(s: Any, now: float | None = None) -> int:
    """Rebuild the view of every active market from the tick memory; returns how many markets it holds. Memory only (the tick history and the strikes the tape listing filled in); a failure on one market leaves the rest."""
    from . import binaryrisk
    from . import ticks as ticks_mod

    global _built_at
    now = time.time() if now is None else now
    fresh: dict[str, dict[str, Any]] = {}
    for ticker in ticks_mod.recent_15m_tickers(s, now, hours=0.5):
        try:
            series = ticker.split("-")[0]
            index = binaryrisk.curve_index(series)
            if not index:
                continue
            got = ticks_mod.history.window(index, now - HOUR - 60.0, now)
            if not got or not got[0]:
                continue
            strike = ticks_mod.strikes.get(ticker) or 0.0
            if strike <= 0:
                strike = binaryrisk.open_strike(got[0], got[1], _close_of(ticker, now, got)) or 0.0
            view = build_one(got[0], got[1], now, float(strike))
            if view is not None:
                view["built_at"] = now
                fresh[ticker] = view
        except Exception:
            log.debug("hourview: could not build %s", ticker, exc_info=True)
    with _lock:
        _VIEW.clear()
        _VIEW.update(fresh)
        _built_at = now
    return len(fresh)


def _close_of(ticker: str, now: float, got: Any) -> float:
    """The close of a market from its ticker (SERIES-YYMONDDHHMM-MM, Eastern time), for the strike a commodity market takes from the index at its open; 0.0 when it cannot be read."""
    import re
    from datetime import datetime
    from zoneinfo import ZoneInfo

    m = re.match(r"^[A-Z0-9]+-(\d\d[A-Z]{3}\d\d)(\d{4})-", ticker)
    if not m:
        return 0.0
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%y%b%d%H%M").replace(tzinfo=ZoneInfo("America/New_York")).timestamp()
    except ValueError:
        return 0.0


def maybe_refresh(s: Any, now: float | None = None) -> bool:
    """Background: rebuild the view when it is EVERY seconds old. True when it ran."""
    now = time.time() if now is None else now
    if now - _built_at < EVERY:
        return False
    refresh(s, now)
    return True


def read(ticker: str, side: str, now: float | None = None) -> dict[str, Any] | None:
    """The hour view of a market for the bet's side, from memory: drift (positive = the hour went the bet's way), range, cross, eff, hi_ago and lo_ago (minutes since the side's best and worst), age (seconds since it was built). None when the market has no view or it is older than twice EVERY."""
    now = time.time() if now is None else now
    with _lock:
        v = _VIEW.get(ticker)
    if v is None or now - v["built_at"] > 2.0 * EVERY:
        return None
    sg = 1.0 if side == "yes" else -1.0
    hi, lo = ("hi_yes_ago", "lo_yes_ago") if side == "yes" else ("lo_yes_ago", "hi_yes_ago")
    bins = [round(sg * x, 3) for x in v.get("bins_yes", [])]
    return {"drift": round(sg * v["drift_yes"], 2), "range": round(v["range"], 2), "cross": v["cross"], "eff": round(v["eff"], 3), "hi_ago": v[hi], "lo_ago": v[lo], "age": round(now - v["built_at"], 0),
            "bins": bins, "bounce": round(bins[-1] - bins[-2], 3) if len(bins) >= 2 else 0.0, "lower_highs": 1.0 if lower_highs(bins) else 0.0}


HOUR_DOWN = -3.5  # the hour's drift in the bet's direction, in typical minutes, at the lower third of the 6,639 PriskP buys of 11-09 to 11-23 (a cut chosen from that sample, said so; scripts/priskp_ridge_study.py)
BOUNCE_MIN = 1.0  # the last 5-minute average up at least one typical minute on the one before: "bought a bounce"; my choice, not scored


def lower_highs(bins: list[float]) -> bool:
    """Three or more interior peaks of the 5-minute averages (the bet's direction), each at least half a typical minute under the one before: the hour is a staircase of failed bounces (user, on BTC 13:45, 2026-11-24: "its a trend down, and we bought UP")."""
    p = [i for i in range(1, len(bins) - 1) if bins[i] > bins[i - 1] and bins[i] >= bins[i + 1]]
    return len(p) >= 3 and all(bins[p[k + 1]] < bins[p[k]] - 0.5 for k in range(len(p) - 1))


def would_skip(hv: dict[str, Any]) -> bool:
    """The SHADOW verdict (rule 5; nothing is refused): the hour went against the bet (drift at or under HOUR_DOWN) and the buy is at a bounce (at least BOUNCE_MIN up on the 5 minutes before)."""
    return hv["drift"] <= HOUR_DOWN and hv.get("bounce", 0.0) >= BOUNCE_MIN


def clear() -> None:
    global _built_at
    with _lock:
        _VIEW.clear()
        _built_at = 0.0
