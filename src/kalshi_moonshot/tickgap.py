"""The per-tick curve gap buy (2026-11-12, user: "design per-tick first"; docs/per-tick-curve-design.md; PAPER shadow lane commodity_multiscale_tick).

One number, read every time the lane looks at a market side: P, the curve's chance that the side settles at $1 (curvevalue.read at the 900+180 scales, pessimistic of the two), and the GAP = P - (ask + entry fee). The curve exit already sells on the same P, so entry and exit now share it.
  BUY     the gap is at or above the side's own cutoff (the `tick_buy_quantile` of that side's gap readings over its last `tick_buy_hours` hours, floored at 0 because the fee is already in the gap; refitted hourly) on this reading and has been for `tick_buy_persist` seconds.
  ANOMALY a reading beyond the side's own 1st or 99th percentile of gap, or whose curve rate is over three times the side's own median: logged to tickgap_anomalies.jsonl with the curve's readings and not bought (CLAUDE.md rule 3). Phase 1 only sits them out; the log is for studying them (studies/results/2026-11-12-tick-gap-anomaly.txt is the first look: 29 extreme-gap signals, too few to say).
  WARM-UP fewer than MIN_HIST readings of the side in the window: no buy. The history is saved to DATA_DIR/tickgap_history.json every ten minutes and reloaded on start, so a restart does not start the warm-up over.
Nothing here names a coin or a commodity: the history is kept per series only because each market's curve is its own, and the cutoffs are quantiles of it. The fixed numbers are the stated ones in the design (80th percentile, 6 h, 5 s, 200 readings) and the 1st/99th/3x of rule 3; none was tuned on results.
"""

from __future__ import annotations

import bisect
import collections
import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import Any, NamedTuple

from . import curvevalue
from . import ticks as ticks_mod
from . import variants
from .strategy import fee_per_contract

log = logging.getLogger("moonshot")

MIN_HIST = 200
REFIT_S = 3600.0
SAVE_S = 600.0
RUN_BREAK_S = 30.0  # a run of cleared readings is broken when the next reading comes later than this
RATE_X = 3.0  # rule 3: a curve rate over three times its own median
LOW_Q, HIGH_Q = 0.01, 0.99  # rule 3: the 1st and 99th percentile of the side's own gap

_lock = threading.Lock()
_hist: dict[str, collections.deque] = {}  # "series|side" -> (ts, gap, rate)
_fit: dict[str, "Fit"] = {}
_run: dict[str, float] = {}  # "ticker|side" -> start of the unbroken run of readings at or above the cutoff
_last: dict[str, float] = {}  # "ticker|side" -> time of the last reading
_feed: dict[str, collections.deque] = {}  # series -> (ts, ticks in the last 60 s, age of the newest tick, median gap between ticks)
_state = {"loaded": False, "saved": 0.0}


class Fit(NamedTuple):
    at: float
    low: float
    cut: float
    high: float
    med_rate: float


class Verdict(NamedTuple):
    ok: bool
    note: str
    status: str  # "clean", "extreme_gap", "high_rate", "warm-up", "unreadable", "below", "waiting"


def _quant(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def _path(s: Any, name: str) -> Path:
    return Path(s.data_dir) / name


def _load(s: Any) -> None:
    if _state["loaded"]:
        return
    _state["loaded"] = True
    try:
        raw = json.loads(_path(s, "tickgap_history.json").read_text())
        for key, rows in raw.items():
            _hist[key] = collections.deque((float(a), float(b), float(c)) for a, b, c in rows)
    except Exception:
        pass


def _save(s: Any, now: float, hours: float) -> None:
    if now - _state["saved"] < SAVE_S:
        return
    _state["saved"] = now
    try:
        out = {}
        for key, dq in _hist.items():
            while dq and dq[0][0] < now - hours * 3600.0:
                dq.popleft()
            out[key] = list(dq)
        p = _path(s, "tickgap_history.json")
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(out))
        tmp.replace(p)
    except Exception as exc:
        log.warning("tick gap history not saved: %s", exc)


def sample(series: str, side: str, left: float, ask: float, now: float, strike: float | None, source: str = "kalshi") -> tuple[curvevalue.Curve, float, tuple[int, float, float]] | None:
    """(the curve, the gap, the feed's (ticks in the last 60 s, newest tick's age, median gap between ticks)) for this side now, or None when it cannot be read."""
    index = variants.spot_index(series, source, now)
    if not index or left <= 0 or left > 900:
        return None
    got = ticks_mod.history.window(index, now - 1000.0, now)
    if not got or not got[0]:
        return None
    ts, vs = got
    if variants.crypto_index(series):
        ref = float(strike or 0.0)
    else:
        opened = now + left - 900.0
        i = bisect.bisect_left(ts, opened - 5.0)
        ref = vs[i] if i < len(ts) and ts[i] <= opened + 20.0 else 0.0
    if ref <= 0:
        return None
    read = curvevalue.read(ts, vs, now, ref, side, left, "900+180")
    if read is None:
        return None
    j = bisect.bisect_right(ts, now)
    cnt = j - bisect.bisect_right(ts, now - 60.0)
    age = now - ts[j - 1]
    steps = [b - a for a, b in zip(ts[max(0, j - 121) : j - 1], ts[max(1, j - 120) : j])]
    med = _quant(steps, 0.5) if steps else 0.0
    return read, read.p - (ask + fee_per_contract(ask)), (cnt, age, med)


def _fit_for(key: str, now: float, hours: float, quantile: float) -> Fit | None:
    f = _fit.get(key)
    if f is not None and now - f.at < REFIT_S:
        return f
    dq = _hist.get(key)
    if dq is None:
        return None
    while dq and dq[0][0] < now - hours * 3600.0:
        dq.popleft()
    if len(dq) < MIN_HIST:
        return None
    gaps = [x[1] for x in dq]
    f = Fit(now, _quant(gaps, LOW_Q), _quant(gaps, quantile), _quant(gaps, HIGH_Q), _quant([x[2] for x in dq], 0.5))
    _fit[key] = f
    return f


def _troubled(series: str, cnt: int, age: float) -> bool:
    """The feed's own trouble, from its own history: ticks in the last 60 s under half its median, or the newest tick older than three median gaps."""
    rows = _feed.get(series)
    if not rows or len(rows) < 20:
        return False
    cnts = [x[1] for x in rows]
    gaps = [x[3] for x in rows if x[3] > 0]
    return cnt < 0.5 * _quant(cnts, 0.5) or (bool(gaps) and age > 3.0 * _quant(gaps, 0.5))


def decide(s: Any, series: str, ticker: str, side: str, left: float, ask: float, strike: float | None, spec: Any, now: float | None = None) -> Verdict:
    """Whether a tick_buy lane buys this side at `ask` now. Every call is one reading and goes into the side's own history."""
    now = time.time() if now is None else now
    hours = float(spec.tick_buy_hours)
    with _lock:
        _load(s)
        smp = sample(series, side, left, ask, now, strike, spec.spot_source or "kalshi")
        if smp is None:
            return Verdict(False, "tick gap: the curve cannot be read (no index, short history, a stale tick or no reference).", "unreadable")
        read, gap, (cnt, age, med) = smp
        key = f"{series}|{side}"
        tk = f"{ticker}|{side}"
        fit = _fit_for(key, now, hours, spec.tick_buy_quantile)
        troubled = _troubled(series, cnt, age)
        _hist.setdefault(key, collections.deque()).append((now, gap, read.rate_long))
        _feed.setdefault(series, collections.deque()).append((now, cnt, age, med))
        dq = _feed[series]
        while dq and dq[0][0] < now - hours * 3600.0:
            dq.popleft()
        _save(s, now, hours)
        if fit is None:
            return Verdict(False, f"tick gap: warm-up, {len(_hist[key])} of {MIN_HIST} readings of this side so far (gap {gap:+.3f}).", "warm-up")
        cut = max(0.0, fit.cut)
        broke = now - _last.get(tk, -1e9) > RUN_BREAK_S
        _last[tk] = now
        if gap < cut:
            _run.pop(tk, None)
            return Verdict(False, f"tick gap {gap:+.3f} is under this side's own cutoff {cut:+.3f} (curve chance {read.p:.3f}).", "below")
        if broke or tk not in _run:
            _run[tk] = now
        held = now - _run[tk]
        status = "extreme_gap" if (gap > fit.high or gap < fit.low) else ("high_rate" if read.rate_long > RATE_X * fit.med_rate else "clean")
        tag = f"gap {gap:+.3f} over cutoff {cut:+.3f}, held {held:.0f}s"
        if status != "clean":
            _anomaly(s, now, ticker, side, ask, read, gap, fit, cut, status, troubled)
            return Verdict(False, f"tick gap anomaly ({status}): {tag}; the rule sits out and logs it.", status)
        if held < float(spec.tick_buy_persist):
            return Verdict(False, f"tick gap {tag}: waiting for {spec.tick_buy_persist:g}s.", "waiting")
        return Verdict(True, f"tick gap {tag}, curve chance {read.p:.3f}" + (", feed troubled" if troubled else ""), "clean")


def recheck(series: str, side: str, left: float, limit: float, strike: float | None, spec: Any, now: float | None = None) -> bool:
    """A resting bid that did not fill: the gap at the bid's price must still clear the side's own cutoff. Not a new reading, so it does not touch the history."""
    now = time.time() if now is None else now
    with _lock:
        smp = sample(series, side, left, limit, now, strike, spec.spot_source or "kalshi")
        fit = _fit_for(f"{series}|{side}", now, float(spec.tick_buy_hours), spec.tick_buy_quantile)
        if smp is None or fit is None:
            return False
        return smp[1] >= max(0.0, fit.cut)


def _anomaly(s: Any, now: float, ticker: str, side: str, ask: float, read: curvevalue.Curve, gap: float, fit: Fit, cut: float, status: str, troubled: bool) -> None:
    row = {"ts": round(now, 1), "ticker": ticker, "side": side, "ask": ask, "p": round(read.p, 4), "gap": round(gap, 4), "cushion": read.cushion, "rate_long": read.rate_long, "rate_short": read.rate_short,
           "low": round(fit.low, 4), "cut": round(cut, 4), "high": round(fit.high, 4), "med_rate": fit.med_rate, "status": status, "feed_troubled": troubled}
    log.info("tick gap anomaly %s %s: %s, gap %+.3f (own range %+.3f to %+.3f), curve rate %.2e vs median %.2e, feed %s", ticker, side, status, gap, fit.low, fit.high, read.rate_long, fit.med_rate, "troubled" if troubled else "healthy")
    try:
        with _path(s, "tickgap_anomalies.jsonl").open("a") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        pass


def reset() -> None:
    """For tests."""
    _hist.clear(); _fit.clear(); _run.clear(); _last.clear(); _feed.clear()
    _state.update(loaded=False, saved=0.0)
