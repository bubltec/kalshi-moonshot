"""The hour's own correction to the ladder's width, fitted from the windows that settled in the last hour (2026-11-14).

The ladder prices P(yes) = Phi(ln(center / strike) / width) with width = sqrt((k * sigma)^2 + unc^2), where sigma is the perp's own movement over the last hour (curverisk.hour_rate) for the time left. k says whether, in this hour, results have been more decisive than that movement implies (k under 1: one-way, the leader keeps winning) or less (k over 1: choppy, leads do not hold).
k is fitted from the CURRENT HOUR only: the windows that closed in the last FIT_HOURS, every series priced from the Kalshi perp pooled into one fit (one k for the market's present behavior, not one per coin), each window read at minutes 5, 8, 11 and 14 as the ladder would have priced it then, the k on a fixed grid with the lowest log loss. It is refitted as each window settles (REFRESH), and a fit older than MAX_AGE is dropped (k = 1, the hour's movement as it is). Under MIN_SAMPLES readings: no fit, k = 1.
History: built 2026-11-13 per series on SIX hours of outcomes over six hours of 5-minute bars. On 2026-11-14 a one-way afternoon left BTC's k at 0.625 and the lane bought KXBTC15M-26NOV141645 NO at 0.55 on a "65%" chance with 0.14 of a real move as its cushion; it lost. User: "These markets shift in behavior hourly if you're looking at 6 hours to base a decision on right now that is totally irrelevant"; "I do like the idea, and we should probably keep that but it should be based on the current hour. 2 hours at most, but I think Kalshi gives us the hour view for a reason".
"""

from __future__ import annotations

import bisect
import math
from typing import Any

from . import commodities as cm
from . import curverisk
from .kalshifeed import _closes

GRID = tuple(0.5 + 0.125 * i for i in range(13))  # 0.50 .. 2.00
MIN_SAMPLES = 40
FIT_HOURS = 1.0  # the windows that closed in the last hour (the user: the current hour, two at most)
SAMPLE_MINUTES = (5, 8, 11, 14)  # elapsed minutes of the window at which a reading is taken
REFRESH = 900.0  # seconds between refits: once a window, as each one settles
MAX_AGE = 2 * REFRESH  # a fit older than this is not used: k = 1, the hour's own movement

Sample = tuple[float, float, float, int]  # ln(center / strike), the last hour's movement over the time left, the offset's spread, 1 if the market settled yes


def chance(lnd: float, sig: float, unc: float, k: float, floor: float) -> float:
    p = cm.prob_above(math.exp(lnd), 1.0, math.sqrt((k * sig) ** 2 + unc**2))
    return min(max(p, floor), 1.0 - floor)


def log_loss(samples: list[Sample], k: float, floor: float) -> float:
    tot = 0.0
    for lnd, sig, unc, yes in samples:
        p = chance(lnd, sig, unc, k, floor)
        tot -= math.log(p if yes else 1.0 - p)
    return tot / len(samples)


def best_k(samples: list[Sample], default: float, floor: float, grid: tuple[float, ...] = GRID, min_samples: int = MIN_SAMPLES) -> float:
    """The grid k with the lowest log loss on these readings, or `default` when there are too few."""
    if len(samples) < min_samples:
        return default
    return min(grid, key=lambda k: log_loss(samples, k, floor))


def samples_from(settled: list[dict[str, Any]], ts: list[float], vs: list[float], now_ts: float, unc_floor: float) -> list[Sample]:
    """The ladder's own readings on a series' windows that settled in the last FIT_HOURS. `ts`/`vs` are the feed's ticks in time order, back at least FIT_HOURS + 4.5 hours (the movement looks one hour behind each reading; the offset of the perp to the settlement reference is learned from the boundaries of the four hours before it)."""
    from datetime import datetime

    refs_all = cm.boundary_refs(settled)
    out: list[Sample] = []
    for m in settled:
        if m.get("result") not in ("yes", "no") or not m.get("floor_strike"):
            continue
        try:
            close = datetime.fromisoformat(m["close_time"]).timestamp()
        except (KeyError, ValueError, TypeError):
            continue
        if not (now_ts - FIT_HOURS * 3600.0 <= close <= now_ts):
            continue
        strike, opened = float(m["floor_strike"]), close - cm.WINDOW_MINUTES * 60.0
        for minute in SAMPLE_MINUTES:
            t = opened + 60.0 * minute
            i = bisect.bisect_right(ts, t) - 1
            if i < 0 or t - ts[i] > cm.REALTIME_STALE_SECONDS:
                continue
            a4 = bisect.bisect_left(ts, t - 4 * 3600.0)
            mc, mt = _closes(list(zip(ts[a4 : i + 1], vs[a4 : i + 1], strict=True)), 60)
            refs = {T: r for T, r in refs_all.items() if t - cm.WINDOW_MAX_AGE <= T <= t}
            wb = cm.window_basis(refs, {int(x): c for x, c in zip(mt, mc, strict=True)}, t)
            if wb is None:
                continue
            a1 = bisect.bisect_left(ts, t - 3700.0)
            rate = curverisk.hour_rate(ts[a1 : i + 1], vs[a1 : i + 1], t)
            if rate is None or rate <= 0:
                continue
            sig = rate * math.sqrt((close - t) + (t - ts[i]))
            out.append((math.log(vs[i] * math.exp(wb.basis) / strike), sig, max(wb.sd, unc_floor), 1 if m["result"] == "yes" else 0))
    return out


latest: tuple[float, float, int] | None = None  # (k, when fitted, readings): the market's present fit, set by CommodityPricer.learn_widths so the buy's stretch and the sale's squeeze can read it without the pricer


def hour_k(now_ts: float) -> float:
    """The hour's width correction as last fitted, or 1 when there is none fresh enough (MAX_AGE): the market's own hour, not a number picked."""
    fit = latest
    return fit[0] if fit is not None and now_ts - fit[1] <= MAX_AGE else 1.0


NUDGE_FLOOR = 0.5  # a wild hour (k of 2 and over) halves the nudge at most


def nudge_scale(k: float) -> float:
    """How much of its cap a nudge may use this hour (2026-11-15, user: the hourly factor should affect the nudge amount). k is under 1 when the hour's results were more decisive than its own movement and over 1 when less: a near miss of a bar means more when the market has been decisive, so the nudge keeps its whole cap at k of 1 or under and shrinks as 1 / k above it, to NUDGE_FLOOR. The lane's cap stays the ceiling."""
    if k <= 1.0:
        return 1.0
    return max(NUDGE_FLOOR, 1.0 / k)

