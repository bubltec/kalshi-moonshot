"""Per-market behaviour profiles and variant types, refreshed hourly (spot ticks in memory) and daily (the tape), 2026-11-06.

User: every market has its own curve and variance, and that history drives how its lane should act. For each 15-minute series this measures, over several look-backs:
  unit_bp  the typical one-minute move in basis points (the root-mean-square one-second move times sqrt(60))
  cross    how many times the spot crosses its opening price per 15-minute window
  rng      a window's high-low range in typical minutes
  late     the share of windows on the other side of the open at minute 10 as at the close
  eff      |net move| / path length of a window (1 = a straight run, 0 = back and forth)
Markets are then labelled against each other (choppy, sticky, trending, late-flip, quiet, or typical) so a new kind of market shows up as a new label, and the label is written on every decision note.
Hourly profiles read the in-memory tick history; the daily one reads the tape. Both append one line per series to DATA_DIR/market_profiles.jsonl. Nothing here changes a setting: it is the measurement a per-market review reads."""

from __future__ import annotations

import bisect
import itertools
import json
import logging
import math
import statistics
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("moonshot")

HOURLY = 3600.0
DAILY = 86400.0
LOOKBACKS_HOURS = {"hourly": (1.0, 6.0, 12.0), "daily": (24.0, 72.0)}
MIN_WINDOWS = 2  # a look-back with fewer whole 15-minute windows than this is not reported
MIN_TICKS = 600

_lock = threading.Lock()
PROFILES: dict[str, dict[str, Any]] = {}  # series -> {"at": ts, "type": label, "looks": {"6h": {...}}}
_hourly_at = 0.0
_daily_at = 0.0


def profile_from(ts: list[float], vs: list[float], t_end: float, hours: float) -> dict[str, float] | None:
    """The profile of the last `hours` hours of one index, or None when the ticks do not cover enough whole windows."""
    t_start = t_end - hours * 3600.0
    i0, i1 = bisect.bisect_left(ts, t_start), bisect.bisect_right(ts, t_end)
    T, V = ts[i0:i1], vs[i0:i1]
    if len(T) < MIN_TICKS:
        return None
    grid: list[float] = []
    k = 0
    for t in range(int(T[0]), int(T[-1])):
        while k + 1 < len(T) and T[k + 1] <= t:
            k += 1
        grid.append(math.log(V[k]))
    steps = [b - a for a, b in itertools.pairwise(grid)]
    if not steps:
        return None
    unit = math.sqrt(sum(x * x for x in steps) / len(steps)) * math.sqrt(60.0)
    if unit <= 0:
        return None
    base = int(T[0])
    t0 = (base // 900 + 1) * 900
    crosses: list[int] = []
    ranges: list[float] = []
    late: list[int] = []
    effs: list[float] = []
    while t0 + 900 <= int(T[-1]):
        seg = grid[t0 - base: t0 - base + 900]
        if len(seg) < 900:
            break
        rel = [x - seg[0] for x in seg]
        crosses.append(sum(1 for x, y in itertools.pairwise(rel) if (x > 0) != (y > 0) and y != 0))
        ranges.append((max(seg) - min(seg)) / unit)
        late.append(1 if (rel[600] > 0) != (rel[-1] > 0) else 0)
        path = sum(abs(y - x) for x, y in itertools.pairwise(seg))
        effs.append(abs(seg[-1] - seg[0]) / path if path > 0 else 0.0)
        t0 += 900
    n = len(crosses)
    if n < MIN_WINDOWS:
        return None
    return {"n": n, "unit_bp": unit * 1e4, "cross": sum(crosses) / n, "rng": sum(ranges) / n, "late": 100.0 * sum(late) / n, "eff": sum(effs) / n}


def classify(longest: dict[str, dict[str, float]]) -> dict[str, str]:
    """Label each series against the others, from its longest profile. A label says how it differs from the median market: choppy (crosses its open a lot), sticky (rarely), trending (straight runs), late-flip (often reverses after minute 10), quiet (small windows)."""
    if len(longest) < 3:
        return dict.fromkeys(longest, "typical")
    med = {key: statistics.median(p[key] for p in longest.values()) for key in ("cross", "eff", "late", "rng")}
    out: dict[str, str] = {}
    for ser, p in longest.items():
        tags = []
        if med["cross"] > 0 and p["cross"] >= 1.25 * med["cross"]:
            tags.append("choppy")
        elif med["cross"] > 0 and p["cross"] <= 0.6 * med["cross"]:
            tags.append("sticky")
        if med["eff"] > 0 and p["eff"] >= 1.5 * med["eff"]:
            tags.append("trending")
        if med["late"] > 0 and p["late"] >= 1.3 * med["late"]:
            tags.append("late-flip")
        if med["rng"] > 0 and p["rng"] <= 0.75 * med["rng"]:
            tags.append("quiet")
        out[ser] = "+".join(tags) if tags else "typical"
    return out


def type_of(series: str) -> str | None:
    with _lock:
        row = PROFILES.get(series)
    return row["type"] if row else None


def snapshot() -> dict[str, dict[str, Any]]:
    with _lock:
        return {k: dict(v) for k, v in PROFILES.items()}


def _index(series: str) -> str | None:
    from . import biquotefeed, kalshiws

    return kalshiws.index_for_series(series) or biquotefeed.index_for_series(series)


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    try:
        with path.open("a") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    except OSError:
        log.debug("market profiles: could not write %s", path, exc_info=True)


def refresh(s: Any, series: list[str], tape: Any = None, now: float | None = None) -> dict[str, dict[str, Any]]:
    """Hourly: profiles from the in-memory ticks (1, 6, 12 h). Daily: profiles from the tape (24, 72 h). Types are re-derived each time from the longest look-back available."""
    global _hourly_at, _daily_at
    from . import ticks as ticks_mod

    now = time.time() if now is None else now
    do_hourly = now - _hourly_at >= HOURLY
    do_daily = tape is not None and now - _daily_at >= DAILY
    if not do_hourly and not do_daily:
        return snapshot()
    rows: list[dict[str, Any]] = []
    longest: dict[str, dict[str, float]] = {}
    with _lock:
        previous = {k: dict(v.get("looks", {})) for k, v in PROFILES.items()}
    fresh: dict[str, dict[str, dict[str, float]]] = {}
    for ser in series:
        idx = _index(ser)
        if not idx:
            continue
        looks = dict(previous.get(ser, {}))
        if do_hourly:
            for h in LOOKBACKS_HOURS["hourly"]:
                got = ticks_mod.history.window(idx, now - h * 3600.0, now)
                p = profile_from(got[0], got[1], now, h) if got else None
                if p:
                    looks[f"{h:g}h"] = p
        if do_daily:
            for h in LOOKBACKS_HOURS["daily"]:
                data = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (idx, now - h * 3600.0 - 60.0)).fetchall()
                p = profile_from([float(r[0]) for r in data], [float(r[1]) for r in data], now, h) if data else None
                if p:
                    looks[f"{h:g}h"] = p
        if looks:
            fresh[ser] = looks
            key = max(looks, key=lambda x: float(x[:-1]))
            longest[ser] = looks[key]
    labels = classify(longest)
    with _lock:
        for ser, looks in fresh.items():
            PROFILES[ser] = {"at": now, "type": labels.get(ser, "typical"), "looks": looks}
    kind = "daily" if do_daily else "hourly"
    for ser, looks in fresh.items():
        rows.append({"ts": round(now), "kind": kind, "series": ser, "type": labels.get(ser, "typical"), "looks": {k: {a: round(b, 3) for a, b in v.items()} for k, v in looks.items()}})
    if do_hourly:
        _hourly_at = now
    if do_daily:
        _daily_at = now
    _write(Path(s.data_dir) / "market_profiles.jsonl", rows)
    log.info("market profiles (%s): %s", kind, {k: v["type"] for k, v in snapshot().items()})
    return snapshot()
