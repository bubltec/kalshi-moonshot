"""PriskP: the checks model's readings, its own-history fifths and its entry (2026-11-22).

P(risk(P)): five separate yes/no checks, each read against that series' own previous day in the same three minutes of the market (CLAUDE.md rules 2 and 3), no single blended score. The numbers here are the ones scripts/priskp_run.py measured (studies/results/2026-11-22-priskp-crypto-run.txt, ...-entry-study.txt); the study and the lane call the same functions, so they read the same way.

THE CHECKS, in the side's view, in the market's own typical minute u15 (the last 15 minutes' root-mean-square one-tick log move per minute):
  cushion   binaryrisk's blended 1/3/5-minute average against the strike, over u15 x sqrt(minutes left)
  trend     the 1-minute average against the 3-minute average, over u15
  history   the share of the last 30 minutes the spot spent on the side's winning side
  price     the side's mid now against up to 3 minutes ago, in the price's own typical minute (rms 5-second change x sqrt(12))
  pricedip  how far the side's mid is under its high of up to the last 5 minutes, in the same unit

A reading's FIFTH is its place among the series' readings of the previous 24 hours in the same bucket of the market (bucket = minutes since the open // 3, at most 4), with cutoffs at the 20th, 40th, 60th and 80th percentiles; beyond the 1st or 99th is ANOMALY; with fewer than NEED readings the check is UNREAD. A check passes in fifths 4 and 5. The cutoffs are built by a background job into a file (cutoffs()/load()); nothing here reads a table.

THE ENTRY (E5 in scripts/priskp_entry_study.py): a side is a candidate at a reading when all five checks are read and pass, its cushion is in fifth 5, and it is a candidate at the reading (priskp.need_readings: one; readings are EVERY seconds apart). A buy is a take at the ask. The window (minutes 2 to 14 after the open) and the dead ask band (0.45 to 0.55) are the user's and are read by the buy step, not here.

Memory only, stdlib only: no table, no file read on a tick.
"""

from __future__ import annotations

import bisect
import itertools
import json
import math
import threading
from types import SimpleNamespace
from typing import Any

from . import binaryrisk

CHECKS = ("cushion", "trend", "history", "price", "pricedip")
QS = (0.01, 0.2, 0.4, 0.6, 0.8, 0.99)  # the cutoffs: the 1st and 99th bound the anomaly, the 20th to 80th cut the fifths
NEED = 100  # readings a bucket needs before its checks are read (a shorter history is UNREAD, not a pass)
EVERY = 5.0  # seconds between two cuts of one market's readings (the step is asked on every tick; the ask is live on every look; user, 2026-11-25: "the only thing we need evaluated every tick, is the market pricing. others can be throttled"); 2026-11-25, user: "it should be like 5s max" (was 10 s), then "that's from the old REST logic... it makes no sense in a websocket" (5 s, then 1 s). Watch `tick latency`: the cut is the heavy part
CONFIRM = 1  # readings in a row a side must be a candidate: ONE, the first candidate reading buys (2026-11-25, user: "thats problematic, i think with our new wave logic that helps wait we should maybe disable that or drop it to 2 readings", "and it needs to scale too", "later in the market it needs to move quicker", "if its taking 20 seconds to make a decision that too long", "it should be like 5s max"; it was three readings, a fixed 20 to 30 s in which the ask could leave the 0.55 to 0.85 band, as BTC YES 12:33 did). A decision is never more than one reading, EVERY seconds, old
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
                ch = [b - a for a, b in itertools.pairwise(mids)]
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


def need_readings(left: float) -> int:
    """Readings in a row a side must be a candidate: CONFIRM (one), whatever the time left. Kept as a function so the step's note and a later scaled rule have one place to ask."""
    return CONFIRM


def confirmed(ticker: str, side: str, ok: bool, now: float, need: int) -> tuple[bool, int]:
    """Count the readings in a row at which a side has been a candidate (one per EVERY seconds however often asked); (`need` reached, the count). A reading that is not a candidate, or a gap of two readings or more, starts the count again."""
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
        return n >= need, n


def note(side: str, states: dict[str, int | str], count: int, minute: float, need: int) -> dict[str, Any]:
    """The market note for one side at one reading, as PROPERTIES the bot can read (user, 2026-11-22: "it could be more yaml like so it's just properties"). A later reading, a sell step and the review page read the keys; render() writes it as YAML-like lines for a log or a decision's note and parse() reads those lines back.

        verb: refuse | wait | buy
        side: yes | no
        minute: 5.0                 minutes since the open
        why: trend_low | unread | cushion_not_strong | confirming | window | pass
        failing: [trend, price]     the checks not in fifths 4-5 (empty when none)
        cushion_fifth: 5            1-5, or anomaly / unread
        confirm: 1                  readings in a row so far
        need: 2                     readings in a row needed
        <check>_fifth: ...          each of the five checks' fifth
    """
    d: dict[str, Any] = {"verb": "", "side": side, "minute": round(minute, 2), "why": "", "failing": [c for c in CHECKS if states[c] not in (4, 5)], "cushion_fifth": states["cushion"], "confirm": count, "need": need}
    for c in CHECKS:
        d[f"{c}_fifth"] = states[c]
    if minute < 2.0 or minute > 14.0:
        d["verb"], d["why"] = "wait", "window"
    elif d["failing"]:
        d["verb"], d["why"] = "refuse", "unread" if any(states[c] == "unread" for c in d["failing"]) else "check_not_passing"
    elif states["cushion"] != 5:
        d["verb"], d["why"] = "wait", "cushion_not_strong"
    elif count < need:
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
    d = [b - a for a, b in itertools.pairwise(lv)]
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


# ---- the buy step ----------------------------------------------------------------------------------------------------------

STEP = "priskp"  # the buy step's name (buychain.py)
WINDOW = (2.0, 14.0)  # the user's: minutes since the open ("like 2-14")
HELD = 0.5  # in a curve, the least chance of ending on the bet's side (from the blended cushion) at which a trend entry buys: under it the move has only just crossed the target and has not held (structural: the cushion is on the bet's side or it is not)
DEAD = (0.45, 0.55)  # the user's own rule for this model: never an ask here
_read_cache: dict[str, tuple[int, dict[str, Any] | None, list[dict[str, float]], float]] = {}  # ticker -> (the EVERY-second bucket it was read in, the readings, the shifts, the strike): a tick asks every 250 ms, the readings are cut once a bucket (CLAUDE.md: under 60 ms a tick)


def wanted(specs: Any) -> bool:
    """Whether any enabled lane names the buy step (the watcher builds and loads the cutoffs only then)."""
    return any(sp.enabled and STEP in sp.buy_chain for sp in specs.values())


def file_path(s: Any) -> Any:
    from pathlib import Path

    return Path(s.data_dir) / "priskp_cutoffs.json"


def refresh(s: Any, now: float | None = None) -> int:
    """Background: install the cutoffs file into memory when it has changed (checked at most once a minute). Returns how many keys are loaded."""
    import time

    global _checked_at, _file_mtime
    now = time.time() if now is None else now
    if now - _checked_at < 60.0:
        return loaded()
    _checked_at = now
    try:
        path = file_path(s)
        mt = path.stat().st_mtime
        if mt != _file_mtime:
            d = json.loads(path.read_text())
            load(d["cuts"], float(d.get("built_at", 0.0)))
            _file_mtime = mt
    except (OSError, ValueError, KeyError):
        pass
    return loaded()


_checked_at = 0.0
_file_mtime = 0.0


def _readings(shot: Any, now: float, hours: float) -> tuple[dict[str, Any] | None, list[dict[str, float]], float, str]:
    """(both sides' readings, the confirmed shifts, the strike, why not): from memory, cut once per EVERY seconds for a market."""
    from . import ticks as ticks_mod

    bucket_id = int(now // EVERY)
    got = _read_cache.get(shot.ticker)
    if got is not None and got[0] == bucket_id:
        return (got[1], got[2], got[3], "") if got[1] is not None else (None, [], 0.0, "the curve cannot be read yet")
    index = binaryrisk.curve_index(shot.series)
    if not index:
        return None, [], 0.0, f"no settlement index for {shot.series}"
    ticks = ticks_mod.history.window(index, now - 3700.0, now)
    strike = ticks_mod.strikes.get(shot.ticker)
    if not ticks or not ticks[0] or not strike or strike <= 0:
        return None, [], 0.0, "no tick history or strike in memory yet"
    T, V = ticks
    close = now + hours * 3600.0
    opened = close - 900.0
    ro = side_readings(T, V, ticks_mod.mids.quotes(shot.ticker), opened, now, float(strike), close)
    sh: list[dict[str, float]] = []
    if ro is not None:
        ux = typical_minute(T, V, now)
        sh = shifts(T, V, now, ux, step_s=30.0) if ux > 0 else []
    _read_cache[shot.ticker] = (bucket_id, ro, sh, float(strike))
    if len(_read_cache) > 512:
        for k in [k for k, v in _read_cache.items() if bucket_id - v[0] > 360]:
            del _read_cache[k]
    return (ro, sh, float(strike), "") if ro is not None else (None, [], 0.0, "the curve cannot be read yet (short or stale history)")


def admit(s: Any, shot: Any, now: float, hours: float | None = None) -> tuple[bool, str]:
    """The buy step's answer for one side now: (ok, the note as one line). Memory only.

    The rules are the user's and the study's: minutes 2 to 14; never an ask of 0.45 to 0.55; all five checks read and in fifths 4-5 with the cushion in fifth 5 (E5), held for the readings the time left asks; and no buy on the side a confirmed shift hurt: the LATEST confirmed shift took the spot out of this side's cushion against this market's target (user, 2026-11-22). A take at the ask is the lane's order.

    The step reads the facts board (facts.py, 2026-11-22: the buy side adopts it): the checks are the fact `priskp_screen`, the shift is the fact `shift_out`, and a veto rule (not the step's own `if`) stops the buy when the shift took the spot out of the side's cushion. The note carries the facts read."""
    from . import facts

    hours = shot.hours_to_close if hours is None else hours  # the hours to the close as of this moment (the shot's own figure is from when it was listed)
    host = SimpleNamespace(s=s, hours=hours, now=now)
    board = facts.Board(host, shot, now, hours * 3600.0, float(getattr(shot, "bid", 0.0) or 0.0))
    view = board.view("priskp")
    n = view.get("priskp_screen")
    if n["verb"] == "buy":
        label = board.vetoes("priskp")
        if label == "early_swings_not_cleared":
            n["verb"], n["why"] = "wait", label  # a WAIT, not a refusal: the entry is bought once the swing-aware chance clears its bar, which slides down with the time left
        elif label:
            n["verb"], n["why"] = "refuse", label
            n["shift_ago_min"] = round(view.get("shift_out")["ago"] / 60.0, 1)
    hv = view.get("hour_view") if n["verb"] == "buy" else None  # the hour before now, from memory (hourview.py): logged only, a tie-breaker the user wants for a risk that is not sure
    if hv is not None:
        n["hour_drift"] = hv["drift"]
        n["hour_tie"] = "unsure" if 4 in [n.get(f"{c}_fifth") for c in CHECKS] else "sure"  # a check in fifth 4 and not 5: the risk is not sure; the hour would speak there
        from . import hourview

        n["hour_bounce"], n["hour_lower_highs"] = hv.get("bounce", 0.0), int(hv.get("lower_highs", 0.0))
        n["hour_shadow"] = "would skip" if hourview.would_skip(hv) else "ok"  # 2026-11-24 (user, on the 14:00 ET wave, five YES buys in a stagnant hour trending down: "the risk should have been high considering the hour view"): a SHADOW verdict, nothing is refused; scored on the bets that follow (rule 5)
    pg = view.get("price_gap")
    if pg is not None:  # an INFLUENCE, not a gate (2026-11-26, user: "it should have an influence but it shouldn't be the factor in whether we buy", "now we have a fact board and vetoes and stuff"): the gap between the bet's chance and what it costs goes on the board's ledger as a signed weight; nothing here buys or refuses on the total (the board's own rule: no step acts on the ledger until the user chooses)
        view.weigh(pg["gap"], f"the chance {pg['chance']:.2f} ({'swing-aware' if pg['swing'] is not None else 'random walk'}) against the ask {pg['ask']:.2f} and its fee")
    return n["verb"] == "buy", one_line(n) + board.note()


def _curve_verdict(shot: Any, now: float, hours: float, strike: float) -> dict[str, Any] | None:
    """The curve verdict (wavefacts.curve) for the shot's side now, or None when it cannot be read: the lane's own checks then decide."""
    from . import binaryrisk, wavefacts

    try:
        index = binaryrisk.curve_index(shot.series or shot.ticker.split("-")[0])
        if not index or strike <= 0:
            return None
        opened = round((now + hours * 3600.0) / 900.0) * 900.0 - 900.0
        return wavefacts.curve(index, opened, now, float(strike), shot.side, max(hours * 3600.0, 1.0), wavefacts.HOUR)  # THE WALK STARTS AN HOUR BEFORE THE OPEN, not at it (2026-11-27, user: "Why doesn't it read before the markets open? It buys starting at 3 mins how would it know if it's not inside a curve?"; "Can we make it look at least 10mins back?"; "the current one that only looks at the market start"). Walked from the open, a leg that began before it was aged from the open, so a move that had already run a usual leg's length read as young and was bought. On the lane's 133 curve buys of 11-25 16:32 to 11-26 22:56 ET (scripts/priskp_leg_walk_back_table.py) the longer walk waits on 36 as "the leg is at its extreme": those settled their way 69% and made -$4.85 held (-$18.05 as run); the 97 it still buys 76% and +$129.62 (+$22.49). The same 36 whether the walk starts 10, 30 or 60 minutes back, so the figure is not doing the work; the hour is what the reading's typical minute and usual leg already use. It can only be shown waiting, not what it would buy later: score the curve buys forward
    except Exception:
        return None


def _nest(shot: Any, now: float, hours: float) -> dict[str, Any] | None:
    """The nested-swings reading (wavefacts.nest) for the shot's side now, or None when it cannot be made."""
    from . import binaryrisk, wavefacts

    try:
        index = binaryrisk.curve_index(shot.series or shot.ticker.split("-")[0])
        if not index:
            return None
        opened = round((now + hours * 3600.0) / 900.0) * 900.0 - 900.0
        return wavefacts.nest(index, opened, now, shot.side)
    except Exception:
        return None


def _nest_note(shot: Any, now: float, hours: float, buying: bool, got: dict[str, Any] | None | bool = False) -> str | None:
    """SHADOW (2026-11-26, user, on recording the nested-swings reading on every buy and logging what it would do before it decides anything: "Ok I like that"): how the market's own swings nest for the shot's side (wavefacts.nest), and what the reading would have said of a buy. On 210 live bets a buy with 3 of the 4 levels running its way settled its way 84% of the time; with 2 of 4 (a bounce against the larger move) 62% and with 4 of 4 (the move fully run) 71%, both negative on both days. It decides nothing: the note only says so. None when the reading cannot be made."""
    if got is False:
        got = _nest(shot, now, hours)
    if got is None:
        return None
    would = "" if not buying else {2: "; shadow: would WAIT (a bounce against the larger move)", 4: "; shadow: would WAIT (the move has fully run)"}.get(got["with"], "; shadow: would buy too")
    if buying and got.get("at_peak"):  # only where the peak wait does not act (outside a curve): 2026-11-27, user: "It also bought on a peak which it should never do for a favorite"
        would += "; shadow: would WAIT for a pullback (bought at the peak of its own swing)"
    big = "n/a" if got["biggest_u"] is None else f"{got['biggest_u']:g} u"
    peak = "at a peak" if got.get("at_peak") else "off the peak"
    return f"{got['signs']} ({got['with']} of 4 with the bet, biggest first; the biggest swing in view {big}; {peak}){would}"


def screen(host: Any, shot: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The fact `priskp_screen`: the five checks for the shot's side now, as the note dict (verb buy / wait), and the latest confirmed shift (or None) for the fact `shift_out`. No shift rule here: the board's veto does that."""
    now, hours = host.now, host.hours
    close = now + hours * 3600.0
    minute = (now - (close - 900.0)) / 60.0
    ro, sh, strike, why = _readings(shot, now, hours)
    if ro is None:
        return {"verb": "wait", "side": shot.side, "minute": round(minute, 2), "why": why}, None
    if loaded() == 0:
        return {"verb": "wait", "side": shot.side, "minute": round(minute, 2), "why": "no cutoffs file loaded yet"}, None
    states = fifths(shot.series, minute * 60.0, ro[shot.side])
    inwin = WINDOW[0] <= minute <= WINDOW[1] and not DEAD[0] <= shot.ask <= DEAD[1]
    cv = _curve_verdict(shot, now, hours, strike)
    # the readings in a row count what DECIDES the buy: in a curve that is the trend entry, elsewhere the five checks. (Until 2026-11-26 the count
    # read the five checks in a curve too, so a curve 'buy' whose checks did not also pass stayed at 0 of 1 'confirming' for ever: 4027 looks on 27
    # markets in three hours, one bet.)
    z = ro[shot.side].get("cushion")
    chance = 0.5 * (1.0 + math.erf(float(z) / math.sqrt(2.0))) if z is not None else None  # the Gaussian chance of ending on the bet's side: the blended cushion IS that z, over the typical minute and the time left
    # In a curve a trend entry WAITS while the lane's own chance for the bet's side is under HELD (0.5: the blended cushion has not yet come over to the
    # bet's side). 2026-11-26, user, on SOL 11:15 NO (bought at the bottom of a drop 8 s after the spot crossed the target, chance 0.38, the spot came
    # straight back) and ZEC 11:30 YES (bought at the top of a bounce, chance 0.46, -$11.60): "They should have bought yes by waiting". A move that has
    # only just crossed the target has not held; the seven other trend entries of those two rounds read 0.52 to 0.81 and all won their side. Nine bets,
    # the bar chosen after seeing them: scored forward. A wait, not a refusal: the bet is bought once the move has held. An unread chance waits nothing.
    held = chance is None or chance >= HELD
    in_curve = cv is not None and cv["regime"] == "curve"
    # IN A CURVE THE LANE DOES NOT BUY AT THE PEAK OF THE BET'S OWN SWING: IT WAITS FOR A PULLBACK (2026-11-27 ~01:00 ET, user, on ETH 00:45 YES bought at the top
    # of a bounce: "It also bought on a peak which it should never do for a favorite"; on the shadow note: "I think we should set the off the peak after a
    # pullback on. It will change when we bet which is better"). AT A PEAK (wavefacts.nest at_peak): at the quarter and at the eighth of the market's own
    # biggest swing, the swing runs the bet's way and has pulled back under a tenth of its size. The trend entry buys INTO the running swing, so most of its
    # buys were at one. On the lane's 174 curve buys of 11-25 16:32 to 11-26 22:56 ET (the production reading): at a peak 92 bets, settled their way 68%,
    # -$69.47 held, -$71.56 as run, negative on both days; off the peak 82 bets, 76%, +$146.32 held, +$71.29 as run. Outside a curve (flat with cushion) a
    # buy at a peak did as well as one off it (91% and 93%), so the wait is the curve's only. HYPE 00:30, BNB 00:45 and ETH 00:45 of 11-27 were all bought
    # at a peak and lost. A wait, not a refusal: the bet is bought once the swing has pulled back and the trend entry still says buy. The table can only
    # show buys the wait holds back, not where the lane then buys: score the curve buys forward. An unread nest waits nothing.
    nest = _nest(shot, now, hours) if inwin else None
    peak = in_curve and nest is not None and bool(nest.get("at_peak"))
    ok = inwin and (cv["verdict"] == "buy" and held and not peak if in_curve else candidate(states))  # type: ignore[index]
    need = need_readings(hours * 3600.0)
    reached, count = confirmed(shot.ticker, shot.side, ok, now, need)
    n = note(shot.side, states, count, minute, need)
    n["ask"] = round(shot.ask, 3)
    if chance is not None:  # SHADOW (2026-11-25, user: "it should have an influence but it shouldn't be the factor in whether we buy"): the Gaussian chance of ending on the bet's side (the blended cushion IS that z, over the typical minute and the time left) against what the bet costs with its fee; logged on every look, decides nothing
        n["chance"] = round(chance, 3)  # the fact `price_gap` (facts.py) turns it into the gap to the ask and weighs it on the board
    if cv is not None:
        n["regime"] = cv["regime"]
        if cv["regime"] == "curve":  # in a curve the trend entry decides, both ways (the five rank checks neither buy nor stop it): wait at a dip or a peak, buy on the trend
            n["curve"] = f"{cv['class']}, swing {cv['swing_u']:.2f} u after a turn of {cv['prev_u']:.2f} u, gave back {cv['gb_u']:.2f}, big leg {cv['age'] if cv['age'] is not None else 'n/a'} and travel {cv['travel'] if cv['travel'] is not None else 'n/a'} of a usual one, target {cv['reach']:.2f} wave travels away"
            if cv["verdict"] == "buy" and inwin and not held:
                n["verb"], n["why"] = "wait", "curve_not_held"  # the swing runs with the bet but the cushion has not come over to its side yet
            elif cv["verdict"] == "buy" and inwin and peak:
                n["verb"], n["why"] = "wait", "curve_peak"  # the bet's own swing is at its top: wait for the pullback
            elif cv["verdict"] == "buy" and inwin:
                n["verb"], n["why"] = "buy", "curve_trend_entry"
                n["failing"] = []
            elif cv["verdict"] == "buy":
                n["verb"], n["why"] = "wait", "window" if not WINDOW[0] <= minute <= WINDOW[1] else "dead_ask_band"
            else:
                n["verb"], n["why"] = "wait", "curve_wait"
    if DEAD[0] <= shot.ask <= DEAD[1]:
        n["verb"], n["why"] = "wait", "dead_ask_band"
    elif n["verb"] == "buy" and not reached:
        n["verb"], n["why"] = "wait", "confirming"
    if inwin:  # the nested swings are read only where the lane may buy (the look's cost is a few milliseconds, shared for 5 s per index)
        told = _nest_note(shot, now, hours, n["verb"] == "buy", nest)
        if told:
            n["nest"] = told
    last = sh[-1] if sh and out_of_cushion(sh[-1], strike, shot.side) else None
    return n, last


def one_line(d: dict[str, Any]) -> str:
    return "; ".join(render(d).splitlines())


# ---- the cutoffs job -------------------------------------------------------------------------------------------------------

REFIT_EVERY = 3600.0
TRAIN_HOURS = 24.0
STEP_S = 10.0
_fitting: Any = None


def maybe_refit(s: Any, now: float | None = None) -> bool:
    """Background: start the hourly rebuild of the cutoffs file as its own low-priority process (python -m kalshi_moonshot.priskp). True when one was started."""
    import os
    import subprocess
    import sys
    import time

    global _fitting
    now = time.time() if now is None else now
    if _fitting is not None and _fitting.poll() is None:
        return False
    try:
        if now - file_path(s).stat().st_mtime < REFIT_EVERY:
            return False
    except OSError:
        pass
    _fitting = subprocess.Popen([sys.executable, "-m", "kalshi_moonshot.priskp"], preexec_fn=lambda: os.nice(15))  # lowers the child's priority only
    return True


def build_samples(s: Any, now: float) -> dict[tuple[str, int, str], list[float]]:
    """The readings of the last TRAIN_HOURS from the tape: every STEP_S seconds of every settled crypto 15-minute market, both sides, keyed (series, bucket, check). The same function that reads the live curve reads the training curve."""
    from . import db
    from .kalshiws import index_for_series

    st = db.connect(s.db_path, "main", shared=True)
    tape = db.connect(s.tape_path, "tape", shared=True)
    wins = st.execute("SELECT ticker, series, close_ts, strike FROM windows WHERE result IN ('yes','no') AND close_ts BETWEEN ? AND ?", (now - TRAIN_HOURS * 3600.0, now)).fetchall()
    by_index: dict[str, list[Any]] = {}
    for w in wins:
        ix = index_for_series(w["series"]) if w["series"].endswith("15M") else None
        if ix:
            by_index.setdefault(ix, []).append(w)
    samples: dict[tuple[str, int, str], list[float]] = {}
    for ix, ws in by_index.items():
        lo, hi = min(float(w["close_ts"]) for w in ws) - 900.0 - 4000.0, max(float(w["close_ts"]) for w in ws) + 5.0
        tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, lo, hi)).fetchall()
        T, V = [float(r[0]) for r in tk], [float(r[1]) for r in tk]
        for w in ws:
            close, opened = float(w["close_ts"]), float(w["close_ts"]) - 900.0
            k = bisect.bisect_left(T, opened)
            strike = float(w["strike"]) if w["strike"] and float(w["strike"]) > 0 else (V[k] if k < len(T) and T[k] - opened <= 25.0 else 0.0)
            if strike <= 0:
                continue
            q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 60.0, close)).fetchall()
            if len(q) < 20:
                continue
            scale = 100.0 if max(float(r[2]) for r in q) > 1.5 else 1.0
            quotes = [(float(r[0]), float(r[1]) / scale, float(r[2]) / scale) for r in q]
            t = opened + 60.0
            while t <= close - 20.0:
                ro = side_readings(T, V, quotes, opened, t, strike, close)
                if ro is not None:
                    b = bucket(t - opened)
                    for side in ("yes", "no"):
                        for c in CHECKS:
                            x = ro[side][c]
                            if x is not None:
                                samples.setdefault((w["series"], b, c), []).append(x)
                t += STEP_S
    return samples


def main() -> int:
    import logging
    import os
    import time
    from pathlib import Path

    from . import config

    log = logging.getLogger("moonshot")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = config.reload()
    now = time.time()
    cuts = cutoffs(build_samples(s, now))
    if not cuts:
        log.warning("priskp: no bucket has %d readings yet, the cutoffs file is not written", NEED)
        return 1
    path = file_path(s)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"built_at": now, "cuts": cuts}))
    os.replace(tmp, Path(path))
    log.info("priskp: cutoffs for %d series buckets written", len(cuts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
