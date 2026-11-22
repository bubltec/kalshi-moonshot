"""PriskP: the checks model's readings, its own-history fifths and its entry (2026-11-22).

P(risk(P)): five separate yes/no checks, each read against that series' own previous day in the same three minutes of the market (CLAUDE.md rules 2 and 3), no single blended score. The numbers here are the ones scripts/priskp_run.py measured (studies/results/2026-11-22-priskp-crypto-run.txt, ...-entry-study.txt); the study and the lane call the same functions, so they read the same way.

THE CHECKS, in the side's view, in the market's own typical minute u15 (the last 15 minutes' root-mean-square one-tick log move per minute):
  cushion   binaryrisk's blended 1/3/5-minute average against the strike, over u15 x sqrt(minutes left)
  trend     the 1-minute average against the 3-minute average, over u15
  history   the share of the last 30 minutes the spot spent on the side's winning side
  price     the side's mid now against up to 3 minutes ago, in the price's own typical minute (rms 5-second change x sqrt(12))
  pricedip  how far the side's mid is under its high of up to the last 5 minutes, in the same unit

A reading's FIFTH is its place among the series' readings of the previous 24 hours in the same bucket of the market (bucket = minutes since the open // 3, at most 4), with cutoffs at the 20th, 40th, 60th and 80th percentiles; beyond the 1st or 99th is ANOMALY; with fewer than NEED readings the check is UNREAD. A check passes in fifths 4 and 5. The cutoffs are built by a background job into a file (cutoffs()/load()); nothing here reads a table.

THE ENTRY (E5 in scripts/priskp_entry_study.py): a side is a candidate at a reading when all five checks are read and pass, its cushion is in fifth 5, and it has been a candidate at three readings in a row (readings are EVERY seconds apart). A buy is a take at the ask. The window (minutes 2 to 14 after the open) and the dead ask band (0.45 to 0.55) are the user's and are read by the buy step, not here.

Memory only, stdlib only: no table, no file read on a tick.
"""

from __future__ import annotations

import bisect
import json
import math
import threading
from collections import deque
from typing import Any

from . import binaryrisk

CHECKS = ("cushion", "trend", "history", "price", "pricedip")
QS = (0.01, 0.2, 0.4, 0.6, 0.8, 0.99)  # the cutoffs: the 1st and 99th bound the anomaly, the 20th to 80th cut the fifths
NEED = 100  # readings a bucket needs before its checks are read (a shorter history is UNREAD, not a pass)
EVERY = 10.0  # seconds between two readings of one market side (the step is asked on every tick)
CONFIRM = 3  # readings in a row a side must be a candidate
BUCKET_S = 180.0  # three minutes of the market


def bucket(seconds_after_open: float) -> int:
    return min(4, int(seconds_after_open // BUCKET_S))


# ---- the readings ----------------------------------------------------------------------------------------------------------


def side_readings(T: list[float], V: list[float], quotes: list[tuple[float, float, float]], opened: float, now: float, strike: float, close: float) -> dict[str, dict[str, float | None]] | None:
    """{"yes": {check: value}, "no": {...}} at `now`, or None when the index history is short or stale. T, V: the settlement index's ticks (ascending, the last at or before now); quotes: (ts, yes_bid, yes_ask) in ascending time from before the open (prices in dollars)."""
    f = binaryrisk.readings(T, V, now, strike, close - now)
    if f is None:
        return None
    j = bisect.bisect_right(T, now) - 1
    lo = bisect.bisect_left(T, now - 1800.0)
    share = sum(1 for v in V[lo : j + 1] if v > strike) / max(1, j + 1 - lo)
    pt = dy = dn = None
    qt = [q[0] for q in quotes]
    if qt:
        first = max(qt[0], opened - 60.0)
        span = min(300.0, now - first)
        if span >= 60.0:
            mids: list[float] = []
            for x in range(int(span // 5), -1, -1):
                k = bisect.bisect_right(qt, now - 5.0 * x) - 1
                if k < 0:
                    mids = []
                    break
                mids.append((quotes[k][1] + quotes[k][2]) / 2.0)
            if len(mids) >= 3:
                ch = [b - a for a, b in zip(mids, mids[1:])]
                pu = math.sqrt(sum(c * c for c in ch) / len(ch) * 12.0)
                if pu > 0:
                    back = min(180.0, span)
                    kb = bisect.bisect_right(qt, now - back) - 1
                    if kb >= 0:
                        mb = (quotes[kb][1] + quotes[kb][2]) / 2.0
                        pt = (mids[-1] - mb) / (pu * math.sqrt(back / 60.0))
                        dy, dn = -(max(mids) - mids[-1]) / pu, -(mids[-1] - min(mids)) / pu
    out: dict[str, dict[str, float | None]] = {}
    for side in ("yes", "no"):
        sg = 1.0 if side == "yes" else -1.0
        out[side] = {
            "cushion": sg * f["czb"],
            "trend": sg * f["tr13"],
            "history": share if side == "yes" else 1.0 - share,
            "price": None if pt is None else sg * pt,
            "pricedip": dy if side == "yes" else dn,
        }
    return out


# ---- the fifths ------------------------------------------------------------------------------------------------------------


def cutoffs(samples: dict[tuple[str, int, str], list[float]]) -> dict[str, list[float]]:
    """The cutoffs file's body from the previous day's readings: {"series|bucket|check": [c1, c20, c40, c60, c80, c99]} for every key with at least NEED readings."""
    out: dict[str, list[float]] = {}
    for (series, b, name), xs in samples.items():
        if len(xs) < NEED:
            continue
        xs = sorted(xs)
        out[f"{series}|{b}|{name}"] = [xs[min(len(xs) - 1, int(len(xs) * q))] for q in QS]
    return out


def fifth(cut: list[float] | None, x: float | None) -> int | str:
    """1 to 5, "anomaly" (beyond the 1st or 99th percentile of the series' own day) or "unread" (no reading, or a bucket with too little history)."""
    if x is None or cut is None:
        return "unread"
    if x < cut[0] or x > cut[5]:
        return "anomaly"
    return 1 + sum(1 for z in cut[1:5] if x > z)


_lock = threading.Lock()
_cuts: dict[str, list[float]] = {}
_built_at = 0.0


def load(body: str | dict[str, Any], built_at: float = 0.0) -> int:
    """Install a cutoffs file's body (JSON text or the dict); returns how many keys it holds."""
    global _cuts, _built_at
    d = json.loads(body) if isinstance(body, str) else body
    with _lock:
        _cuts = {k: [float(x) for x in v] for k, v in d.items() if isinstance(v, list) and len(v) == len(QS)}
        _built_at = built_at
        return len(_cuts)


def loaded() -> int:
    with _lock:
        return len(_cuts)


def fifths(series: str, seconds_after_open: float, readings: dict[str, float | None]) -> dict[str, int | str]:
    b = bucket(seconds_after_open)
    with _lock:
        return {c: fifth(_cuts.get(f"{series}|{b}|{c}"), readings.get(c)) for c in CHECKS}


# ---- the entry -------------------------------------------------------------------------------------------------------------

_runs: dict[tuple[str, str], tuple[float, int]] = {}


def clear() -> None:
    global _cuts
    with _lock:
        _cuts = {}
        _runs.clear()


def out_of_cushion(shift: dict[str, float], strike: float, side: str) -> bool:
    """Whether a confirmed shift took the spot OUT of the side's cushion, read against the current market's target (user, 2026-11-22: no buys on the side of the shift, if it shifted out of cushion): the level before the shift was on the side's winning side of the strike and the level after is not."""
    sg = 1.0 if side == "yes" else -1.0
    return sg * (shift["before"] - strike) > 0 and sg * (shift["after"] - strike) <= 0


def candidate(states: dict[str, int | str]) -> bool:
    """All five checks read and in fifths 4 or 5, with the cushion in fifth 5 (E5's strong cushion)."""
    return all(states[c] in (4, 5) for c in CHECKS) and states["cushion"] == 5


def confirmed(ticker: str, side: str, ok: bool, now: float) -> tuple[bool, int]:
    """Count the readings in a row at which a side has been a candidate (one per EVERY seconds however often asked); (CONFIRM reached, the count). A reading that is not a candidate, or a gap of two readings or more, starts the count again."""
    with _lock:
        t, n = _runs.get((ticker, side), (-1e18, 0))
        if not ok:
            _runs[(ticker, side)] = (now, 0)
            return False, 0
        if now - t >= EVERY * 2.5:
            n = 0
        if now - t >= EVERY - 1e-9 or t < 0:
            n += 1
            t = now
        _runs[(ticker, side)] = (t, n)
        if len(_runs) > 4096:  # markets long closed
            for k in [k for k, (tt, _n) in _runs.items() if now - tt > 3600.0]:
                del _runs[k]
        return n >= CONFIRM, n


def note(side: str, states: dict[str, int | str], count: int, minute: float) -> dict[str, Any]:
    """The market note for one side at one reading, as PROPERTIES the bot can read (user, 2026-11-22: "it could be more yaml like so it's just properties"). A later reading, a sell step and the review page read the keys; render() writes it as YAML-like lines for a log or a decision's note and parse() reads those lines back.

        verb: refuse | wait | buy
        side: yes | no
        minute: 5.0                 minutes since the open
        why: trend_low | unread | cushion_not_strong | confirming | window | pass
        failing: [trend, price]     the checks not in fifths 4-5 (empty when none)
        cushion_fifth: 5            1-5, or anomaly / unread
        confirm: 2                  readings in a row so far
        need: 3                     readings in a row needed
        <check>_fifth: ...          each of the five checks' fifth
    """
    d: dict[str, Any] = {"verb": "", "side": side, "minute": round(minute, 2), "why": "", "failing": [c for c in CHECKS if states[c] not in (4, 5)], "cushion_fifth": states["cushion"], "confirm": count, "need": CONFIRM}
    for c in CHECKS:
        d[f"{c}_fifth"] = states[c]
    if minute < 2.0 or minute > 14.0:
        d["verb"], d["why"] = "wait", "window"
    elif d["failing"]:
        d["verb"], d["why"] = "refuse", "unread" if any(states[c] == "unread" for c in d["failing"]) else "check_not_passing"
    elif states["cushion"] != 5:
        d["verb"], d["why"] = "wait", "cushion_not_strong"
    elif count < CONFIRM:
        d["verb"], d["why"] = "wait", "confirming"
    else:
        d["verb"], d["why"] = "buy", "pass"
    return d


def render(d: dict[str, Any]) -> str:
    """The note as YAML-like lines, one `key: value` each; a list is written [a, b]."""
    out = []
    for k, v in d.items():
        out.append(f"{k}: [{', '.join(str(x) for x in v)}]" if isinstance(v, list) else f"{k}: {v}")
    return "\n".join(out)


def parse(text: str) -> dict[str, Any]:
    """Read render()'s lines back into properties: integers, numbers and lists are restored, anything else stays text."""
    d: dict[str, Any] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            d[k] = [x.strip() for x in v[1:-1].split(",") if x.strip()]
            continue
        try:
            d[k] = int(v)
        except ValueError:
            try:
                d[k] = float(v)
            except ValueError:
                d[k] = v
    return d


def typical_minute(T: list[float], V: list[float], now: float, span: float = 900.0) -> float:
    """The spot's typical minute in index units: the root-mean-square change between ten-second levels over the last `span` seconds, scaled to a minute (change / sqrt(10) x sqrt(60)). 0.0 when the history is short."""
    lv = []
    t = now - span
    while t <= now:
        k = bisect.bisect_right(T, t) - 1
        if k >= 0:
            lv.append(V[k])
        t += 10.0
    if len(lv) < 10:
        return 0.0
    d = [b - a for a, b in zip(lv, lv[1:])]
    return math.sqrt(sum(x * x for x in d) / len(d) * 6.0)


def shifts(T: list[float], V: list[float], now: float, u: float, hold: float = 180.0, zmin: float = 2.0, span: float = 3600.0, step_s: float = 10.0) -> list[dict[str, float]]:
    """The confirmed level shifts in the last `span` seconds of the spot, newest last (user, 2026-11-22: a shift happens at any size and any time frame, needs about 3 to 5 minutes of holding to count, and can repeat or last a long while).

    A shift at time t: the spot's mean level over the `hold` seconds after t differs from its mean over the `hold` seconds before t by more than random noise over the hold would explain (|step| >= zmin x u x sqrt(hold / 60), u the spot's typical minute in index units), AND both levels held: no 10-second level in the hold window after t has gone back past the midpoint of the old and new levels, and none in the window before t has already crossed it (a spike's return is not a shift). t must be at least `hold` seconds ago (a shift is confirmed by the hold that followed it). Candidates within `hold` of each other are merged to the one with the largest step. Returns {ago (seconds), step (new level minus old, in index units), z (the step over the noise), held (the share of the step still in place now, 1.0 = all, negative = it has gone the other way), dir (1 up, -1 down), before and after (the two levels in index units: the caller reads them against the CURRENT market's target, never an earlier window's)}.
    Pure function: ticks T, V ascending, the last at or before `now`."""
    if u <= 0 or not T or now - T[0] < span * 0.5:
        return []
    t0 = max(T[0], now - span)
    grid: list[tuple[float, float]] = []
    t = t0
    while t <= now:
        k = bisect.bisect_right(T, t) - 1
        if k >= 0:
            grid.append((t, V[k]))
        t += step_s
    n = int(hold // step_s)
    if len(grid) < 3 * n:
        return []
    noise = u * math.sqrt(hold / 60.0)
    found: list[dict[str, float]] = []
    for i in range(n, len(grid) - n + 1):
        before = [v for _, v in grid[i - n : i]]
        after = [v for _, v in grid[i : i + n]]
        step = sum(after) / n - sum(before) / n
        if abs(step) < zmin * noise:
            continue
        mid = (sum(after) / n + sum(before) / n) / 2.0
        d = 1 if step > 0 else -1
        if any((v - mid) * d < 0 for v in after) or any((v - mid) * d > 0 for v in before):  # the new level did not hold, or the old one was not itself steady (a spike's return is not a shift)
            continue
        found.append({"ago": now - grid[i][0], "step": step, "z": abs(step) / noise, "dir": float(d), "before": sum(before) / n, "after": sum(after) / n})
    merged: list[dict[str, float]] = []
    for c in sorted(found, key=lambda c: -c["ago"]):  # oldest first
        if merged and merged[-1]["ago"] - c["ago"] < hold:
            if c["z"] > merged[-1]["z"]:
                merged[-1] = c
        else:
            merged.append(c)
    last = grid[-1][1]
    for c in merged:
        old = (V[bisect.bisect_right(T, now - c["ago"]) - 1] if c["ago"] > 0 else last) - c["step"]
        c["held"] = (last - old) / c["step"] if c["step"] else 0.0
    return merged


class Staircase:
    """The sell step's state for one bet (scripts/priskp_staircase_study.py, rule 1 with the swing size K0 = 1.0, chosen after seeing the study): swings of the bid, and the sale once the bid falls under the last confirmed swing low after the bet has been ahead.

    rev = max(0.01, K0 x u x sqrt(max(T, 60) / 60)), u the bid's typical minute. After a high, a pullback of rev confirms the swing high; after that, a bounce of rev off the lowest point confirms a swing low. The sale can be at a small loss; there is no loss cap."""

    K0 = 1.0

    def __init__(self) -> None:
        self.phase = "up"
        self.hi = 0.0
        self.lo = 1.0
        self.swing_low: float | None = None
        self.ahead = False

    def step(self, bid: float, net: float, u: float, left: float) -> bool:
        """Feed one reading (the bid, its gain after both fees per contract, the bid's typical minute, the seconds left); True when the bet is to be sold now."""
        if net > 0:
            self.ahead = True
        rev = max(0.01, self.K0 * u * math.sqrt(max(left, 60.0) / 60.0))
        if self.phase == "up":
            self.hi = max(self.hi, bid)
            if bid <= self.hi - rev:
                self.phase, self.lo = "down", bid
        else:
            self.lo = min(self.lo, bid)
            if bid >= self.lo + rev:
                self.swing_low, self.phase, self.hi = self.lo, "up", bid
        return self.swing_low is not None and bid < self.swing_low and self.ahead


def bid_unit(bids: deque[tuple[float, float]] | list[tuple[float, float]], now: float) -> float:
    """The bid's typical minute from (time, bid) readings: the root-mean-square change between consecutive readings, scaled to a minute by the time between them (change / sqrt(dt) x sqrt(60)), over the last 15 minutes. 0.0 with fewer than 3 changes."""
    rows = [(t, b) for t, b in bids if now - 900.0 <= t <= now]
    d = []
    for (t0, b0), (t1, b1) in zip(rows, rows[1:]):
        dt = t1 - t0
        if dt > 0:
            d.append(((b1 - b0) ** 2) / dt)
    if len(d) < 3:
        return 0.0
    return math.sqrt(sum(d) / len(d) * 60.0)
