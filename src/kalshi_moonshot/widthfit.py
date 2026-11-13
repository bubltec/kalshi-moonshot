"""A 15-minute series' own width multiplier, fitted from its own recent outcomes (2026-11-13, user: "whatever you need to change to make the ladder work"; "Fit from recent outcomes").

The ladder prices P(yes) = Phi(ln(center / strike) / width) with width = sqrt((k * realized_sigma)^2 + unc^2). k was one global number (COMMODITY_VOL_MULT, 1.25), set for the commodity ladders. On every settled crypto 15-minute market since 11-11 (studies/results/2026-11-13-ladder-calibration-all-markets.txt) that width was too wide: Brier 0.1322 against the quote's 0.1215, the offset noise only 1.6% of the width, the best single k about 0.75 and a k fitted per series from its last 6 hours of outcomes (median about 0.875) scoring 0.1271.

Here the series fits its own k, hourly (rule 2: the cutoff is a quantile of the market's own recent history, on a schedule): from the windows that settled in the last FIT_HOURS, each read at minutes 5, 8, 11 and 14 as the ladder would have priced it then (the feed's own ticks up to that moment, the offset learned from the boundaries before it), the k on a fixed grid with the lowest log loss. Under MIN_SAMPLES readings: no fit, the global k stays. Nothing here names a coin: the series is the market, and the fit is for any series priced from the Kalshi perp feed.
"""

from __future__ import annotations

import bisect
import math
from typing import Any

from . import commodities as cm
from .kalshifeed import _closes

GRID = tuple(0.5 + 0.125 * i for i in range(13))  # 0.50 .. 2.00
MIN_SAMPLES = 40
FIT_HOURS = 6.0
SAMPLE_MINUTES = (5, 8, 11, 14)  # elapsed minutes of the window at which a reading is taken
REFRESH = 3600.0  # seconds between refits of a series
MAX_AGE = 3 * REFRESH  # a fit older than this is not used: the global k returns

Sample = tuple[float, float, float, int]  # ln(center / strike), realized sigma, the offset's spread, 1 if the market settled yes


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
    """The ladder's own readings on a series' recently settled windows. `ts`/`vs` are the feed's ticks in time order, back at least FIT_HOURS + 6 hours (the volatility looks 6 hours behind each reading)."""
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
            a6 = bisect.bisect_left(ts, t - 6 * 3600.0)
            closes, times = _closes(list(zip(ts[a6 : i + 1], vs[a6 : i + 1], strict=True)), 300)
            feed = cm.Series(last=vs[i], last_ts=ts[i], closes=closes, times=times)
            a4 = bisect.bisect_left(ts, t - 4 * 3600.0)
            mc, mt = _closes(list(zip(ts[a4 : i + 1], vs[a4 : i + 1], strict=True)), 60)
            refs = {T: r for T, r in refs_all.items() if t - cm.WINDOW_MAX_AGE <= T <= t}
            wb = cm.window_basis(refs, {int(x): c for x, c in zip(mt, mc, strict=True)}, t)
            if wb is None:
                continue
            try:
                sig = cm.realized_sigma(feed, (close - t) / 3600.0 + (t - ts[i]) / 3600.0)
            except cm.CommodityError:
                continue
            out.append((math.log(vs[i] * math.exp(wb.basis) / strike), sig, max(wb.sd, unc_floor), 1 if m["result"] == "yes" else 0))
    return out
