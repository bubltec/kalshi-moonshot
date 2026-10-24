"""A nearest-neighbour model of entries, in place of hard buckets.

grid.py sorts every entry into a bucket (minute band, price band, last-hour trend, market trend) and asks a yes/no question of each bucket. With
hundreds of buckets each holds few entries, so almost nothing clears the bar. This module describes each entry instead as a point in a small space
and asks what happened to the entries *nearest* to the one in front of us:

  coordinates   minute in the window, ask, spread, how the ask moved over the last 90 s and 4 min, the instrument's last-hour move (alpha) and the
                whole market's (beta) as they bear on the side bought, and how far the ask sits under (or over) fair value; each scaled to a
                comparable size, and a flag for whether the price feeds were known
  history       every sampled entry on every recorded window (one every 30 s on each side), with the return it ended up with under the lane's
                exit rule (`trail` = the live crash exit, `hold` = to settlement). Stored in Postgres as a pgvector column, so "the 300 nearest
                entries" is one indexed query; on SQLite (tests) the same search runs in Python.
  estimate      the nearest entries are weighted by closeness (a kernel as wide as the typical neighbour distance), grouped by window and side
                (entries 30 s apart in one window are not independent), and averaged. The standard error comes from the spread between
                those groups, with the effective number of groups.
  decision      buy when the estimate's lower bound (mean - z standard errors) is above zero with enough effective groups.

So it adjusts smoothly: moving the minute, the price, the trend or the gap changes which neighbours count, and the estimate follows, instead of falling off
the edge of a bucket. Nothing here places an order; the lane that uses it is paper unless LIVE_LANES names it.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import db, grid
from .strategy import fee_per_contract as fee

log = logging.getLogger("moonshot")

DIM = 9
SAMPLE_EVERY = 30.0  # seconds between sampled entries inside a window
FIRST, LAST = 30.0, 45.0  # seconds after the open / before the close that sampling covers
UPDATE_EVERY = 600.0
# how much each coordinate counts in the distance (after its own scaling): the ask and the minute matter most, the rest are context
WEIGHTS = (1.0, 2.0, 0.7, 0.8, 0.8, 1.0, 1.0, 1.0, 0.5)


def _clip(x: float, lim: float) -> float:
    return max(-lim, min(lim, x))


def vector(minute: float, ask: float, spread: float, d90: float | None, d240: float | None, alpha: float | None, beta: float | None, gap: float | None) -> list[float]:
    """An entry as a point. alpha, beta and gap are already signed for the side bought (positive = in its favour)."""
    raw = [
        minute / 15.0,
        ask,
        spread * 20.0,
        _clip((d90 or 0.0) * 5.0, 1.5),
        _clip((d240 or 0.0) * 3.0, 1.5),
        _clip((alpha or 0.0) / 2.0, 1.5),
        _clip((beta or 0.0) / 2.0, 1.5),
        _clip(gap or 0.0, 0.5) * 2.0,
        1.0 if alpha is not None else 0.0,
    ]
    return [round(v * w, 4) for v, w in zip(raw, WEIGHTS, strict=True)]


def _lit(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.4f}" for v in vec) + "]"


# ---- storage ---------------------------------------------------------------------------------------------------------------------

def ensure_schema(store: Any) -> bool:
    """Create the tables (and, on Postgres, the pgvector column and its index). Returns False if pgvector is unavailable there."""
    if db.is_pg(store.db):
        try:
            store.db.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public")
            store.db.commit()
        except Exception:
            store.db.rollback()
            log.warning("knn: the vector extension is not available in this Postgres; the nearest-neighbour model is off")
            return False
        store.db.executescript(
            f"""CREATE TABLE IF NOT EXISTS entry_vec (
              id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, series TEXT NOT NULL, side TEXT NOT NULL, close_ts INTEGER NOT NULL,
              rule TEXT NOT NULL, minute REAL NOT NULL, ask REAL NOT NULL, roi REAL NOT NULL, feats vector({DIM}) NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_entry_vec_hnsw ON entry_vec USING hnsw (feats vector_l2_ops);
            CREATE INDEX IF NOT EXISTS idx_entry_vec_rule ON entry_vec (rule, series, close_ts);
            CREATE TABLE IF NOT EXISTS knn_done (ticker TEXT NOT NULL, rule TEXT NOT NULL, PRIMARY KEY (ticker, rule));"""
        )
        return True
    store.db.executescript(
        """CREATE TABLE IF NOT EXISTS entry_vec (
          id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, series TEXT NOT NULL, side TEXT NOT NULL, close_ts INTEGER NOT NULL,
          rule TEXT NOT NULL, minute REAL NOT NULL, ask REAL NOT NULL, roi REAL NOT NULL, feats TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_entry_vec_rule ON entry_vec (rule, series, close_ts);
        CREATE TABLE IF NOT EXISTS knn_done (ticker TEXT NOT NULL, rule TEXT NOT NULL, PRIMARY KEY (ticker, rule));"""
    )
    return True


# ---- features from the tape -------------------------------------------------------------------------------------------------------

def _side_view(rows: list[tuple[float, float, float]], side: str) -> list[tuple[float, float, float]]:
    return [(t, b if side == "yes" else 1.0 - a, a if side == "yes" else 1.0 - b) for t, b, a in rows]


def _ask_change(times: list[float], path: list[tuple[float, float, float]], i: int, seconds: float) -> float | None:
    j = bisect.bisect_right(times, times[i] - seconds) - 1
    return None if j < 0 else path[i][2] - path[j][2]


def entry_vector(series: str, side: str, ts: float, close: float, path: list[tuple[float, float, float]], times: list[float], i: int,
                 feats: grid.Features, strike: float | None) -> list[float]:
    _t, bid, ask = path[i]
    sign = 1.0 if side == "yes" else -1.0
    a = feats.alpha(series, ts)
    b = feats.beta(ts)
    gap = feats.gap(series, ts, strike, (close - ts) / 60.0, ask, side)
    return vector((ts - (close - 900.0)) / 60.0, ask, max(0.0, ask - bid), _ask_change(times, path, i, 90.0), _ask_change(times, path, i, 240.0),
                  None if a is None else a * sign, None if b is None else b * sign, gap)


def update(store: Any, tape_path: Path, series: tuple[str, ...], rule: str = "trail", now: float | None = None, limit: int | None = None) -> int:
    """Sample entries from every settled window not yet done and store them with their outcome under `rule`. Returns windows added."""
    now = now or time.time()
    if not ensure_schema(store):
        return 0
    tape = grid._open_tape(tape_path)
    if tape is None:
        return 0
    feats = grid.Features(tape)
    marks = ",".join("?" * len(series))
    todo = store.db.execute(
        f"SELECT w.ticker, w.series, w.close_ts, w.result, w.strike FROM windows w LEFT JOIN knn_done d ON d.ticker=w.ticker AND d.rule=? "
        f"WHERE d.ticker IS NULL AND w.result IN ('yes','no') AND w.series IN ({marks}) AND w.close_ts < ? ORDER BY w.close_ts",
        (rule, *series, now - 1200.0),
    ).fetchall()
    added = 0
    for w in todo[:limit] if limit else todo:
        close = float(w["close_ts"])
        rows = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts", (w["ticker"],)).fetchall()
        batch: list[tuple] = []
        if rows and rows[0][0] <= close - 900.0 + 120.0:
            base = [(r[0], r[1], r[2]) for r in rows]
            for side in ("yes", "no"):
                path = [p for p in _side_view(base, side) if p[0] < close - 5.0]
                times = [p[0] for p in path]
                t = close - 900.0 + FIRST
                while t < close - LAST:
                    i = bisect.bisect_right(times, t) - 1
                    if i >= 5 and path[i][2] > 0.02 and path[i][2] < 0.98:
                        ask = path[i][2]
                        sold = grid.trail_exit(path[i + 1:], ask) if rule == "trail" else None
                        roi = grid._roi_exit(ask, sold) if sold is not None else (1.0 if w["result"] == side else 0.0) / (ask + fee(ask)) - 1.0
                        vec = entry_vector(w["series"], side, t, close, path, times, i, feats, w["strike"])
                        batch.append((w["ticker"], w["series"], side, int(close), rule, (t - (close - 900.0)) / 60.0, ask, roi, _lit(vec) if db.is_pg(store.db) else json.dumps(vec)))
                    t += SAMPLE_EVERY
        if batch:
            store.db.executemany("INSERT INTO entry_vec (ticker, series, side, close_ts, rule, minute, ask, roi, feats) VALUES (?,?,?,?,?,?,?,?,?)", batch)
            added += 1
        store.db.execute("INSERT OR IGNORE INTO knn_done (ticker, rule) VALUES (?, ?)", (w["ticker"], rule))
        store.db.commit()
    tape.close()
    return added


_lock = threading.Lock()
_last: dict[tuple, float] = {}


def refresh_soon(store_factory: Any, tape_path: Path, series: tuple[str, ...], rule: str = "trail") -> None:
    """Bring the history up to date in the background, at most every UPDATE_EVERY seconds for each series and exit rule."""
    with _lock:
        if time.time() - _last.get((series, rule), 0.0) < UPDATE_EVERY:
            return
        _last[(series, rule)] = time.time()

    def work() -> None:
        try:
            n = update(store_factory(), tape_path, series, rule)
            if n:
                log.info("knn: added %d more windows of entries", n)
        except Exception:
            log.exception("knn: update failed")

    threading.Thread(target=work, name="knn-update", daemon=True).start()


# ---- the search and the estimate --------------------------------------------------------------------------------------------------

def neighbors(store: Any, vec: list[float], series: tuple[str, ...], rule: str, since: float = 0.0, k: int = 300, before: float | None = None) -> list[tuple[str, str, float, float]]:
    """The k nearest stored entries: (ticker, side, return, distance). `before` keeps only windows closing earlier than that (for validation)."""
    marks = ",".join("?" * len(series))
    cut = " AND close_ts < ?" if before is not None else ""
    extra = (before,) if before is not None else ()
    if db.is_pg(store.db):
        lit = _lit(vec)
        store.db.execute("SET hnsw.ef_search = 400")
        rows = store.db.execute(
            f"SELECT ticker, side, roi, feats <-> ?::vector AS d FROM entry_vec WHERE rule=? AND series IN ({marks}) AND close_ts >= ?{cut} "
            f"ORDER BY feats <-> ?::vector LIMIT ?",
            (lit, rule, *series, since, *extra, lit, k),
        ).fetchall()
        return [(r[0], r[1], float(r[2]), float(r[3])) for r in rows]
    out = []
    for r in store.db.execute(
        f"SELECT ticker, side, roi, feats FROM entry_vec WHERE rule=? AND series IN ({marks}) AND close_ts >= ?{cut}", (rule, *series, since, *extra)
    ):
        f = json.loads(r["feats"])
        out.append((r["ticker"], r["side"], float(r["roi"]), math.sqrt(sum((a - b) ** 2 for a, b in zip(f, vec, strict=True)))))
    out.sort(key=lambda x: x[3])
    return out[:k]


def estimate(near: list[tuple[str, str, float, float]]) -> dict[str, float]:
    """Kernel-weighted mean return of the neighbours, with a standard error taken across windows (not across entries)."""
    if len(near) < 5:
        return {"mean": 0.0, "se": float("inf"), "n_eff": 0.0, "groups": 0.0, "bandwidth": 0.0}
    dists = sorted(d for *_x, d in near)
    h = max(dists[len(dists) // 2], 1e-6)
    groups: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])  # (sum of weights, sum of weight*roi, entries)
    for ticker, side, roi, d in near:
        w = math.exp(-0.5 * (d / h) ** 2)
        g = groups[(ticker, side)]
        g[0] += w
        g[1] += w * roi
        g[2] += 1.0
    big_w = [g[0] / g[2] for g in groups.values()]  # a window counts by how close its entries are, not by how many of them are near
    means = [g[1] / g[0] for g in groups.values()]
    sw = sum(big_w)
    mean = sum(w * m for w, m in zip(big_w, means, strict=True)) / sw
    n_eff = sw * sw / sum(w * w for w in big_w)
    denom = sw - sum(w * w for w in big_w) / sw
    var = sum(w * (m - mean) ** 2 for w, m in zip(big_w, means, strict=True)) / denom if denom > 1e-9 else 0.0
    se = math.sqrt(var / n_eff) if n_eff > 1 else float("inf")
    return {"mean": mean, "se": se, "n_eff": n_eff, "groups": float(len(groups)), "bandwidth": h}


def decide(est: dict[str, float], min_n: float, z: float) -> tuple[bool, str]:
    lo = est["mean"] - z * est["se"]
    if est["n_eff"] < min_n or not math.isfinite(est["se"]):
        return False, f"knn: only {est['n_eff']:.0f} comparable windows so far, needs {min_n:g}"
    text = f"{est['mean']:+.1%} over {est['n_eff']:.0f} comparable windows, {z:g} standard errors under is {lo:+.1%}"
    return (lo > 0), ("knn: buy, " if lo > 0 else "knn: pass, ") + text


# ---- live: the decision for an entry about to be made -----------------------------------------------------------------------------

def says(store: Any, spec: Any, tape_path: Path, ticker: str, series: str, side: str, ts: float, close: float, strike: float | None, ask: float,
         since: float = 0.0) -> tuple[bool, str]:
    """Whether the nearest-neighbour model would buy this entry now, and why."""
    names = tuple(spec.series) or (series,)
    if not ensure_schema(store):
        return False, "knn: pgvector is not available"
    refresh_soon(lambda: type(store)(store.path, store.exclude_versions), tape_path, names, spec.knn_rule)
    tape = grid._open_tape(tape_path)
    if tape is None:
        return False, "knn: no tape yet"
    try:
        rows = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts>=? AND ts<=? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts",
                            (ticker, ts - 300.0, ts)).fetchall()
        path = _side_view([(r[0], r[1], r[2]) for r in rows], side)
        if len(path) < 3:
            return False, "knn: too little recent price history for this market"
        times = [p[0] for p in path]
        vec = entry_vector(series, side, ts, close, path, times, len(path) - 1, grid.Features(tape), strike)
    finally:
        tape.close()
    near = neighbors(store, vec, names, spec.knn_rule, since, spec.knn_k)
    est = estimate(near)
    go, why = decide(est, spec.knn_min_n, spec.knn_z)
    return go, f"{why} [k={len(near)}, minute {(ts - (close - 900.0)) / 60:.1f}, ask {ask:.2f}]"


# ---- does it work? an honest test on windows it has not seen -------------------------------------------------------------------------

def _parse_vec(text: Any) -> list[float]:
    return json.loads(text) if isinstance(text, str) else list(text)


def validate(store: Any, series: tuple[str, ...], rule: str, k: int = 200, z: float = 3.0, min_n: float = 25.0, sample: int = 3000,
             train_share: float = 0.6, embargo: float = 1200.0, seed: int = 1, ask_range: tuple[float, float] = (0.0, 1.0)) -> dict[str, Any]:
    """Fit on the earliest `train_share` of windows and test on later ones (leaving a gap so no test window overlaps training in time).

    For each sampled test entry the neighbours come only from training windows, the model makes its buy / pass call, and the entry's real return is
    looked up. Reports the realized return of the entries the model would buy, against all test entries and against a same-sized random pick.
    """
    import random

    marks = ",".join("?" * len(series))
    closes = sorted({r[0] for r in store.db.execute(f"SELECT DISTINCT close_ts FROM entry_vec WHERE rule=? AND series IN ({marks})", (rule, *series))})
    if len(closes) < 50:
        return {"error": "not enough history yet"}
    cut = closes[int(len(closes) * train_share)]
    rows = store.db.execute(
        f"SELECT ticker, side, roi, feats{'::text' if db.is_pg(store.db) else ''} AS f FROM entry_vec WHERE rule=? AND series IN ({marks}) AND close_ts >= ? AND ask >= ? AND ask <= ?",
        (rule, *series, cut + embargo, ask_range[0], ask_range[1]),
    ).fetchall()
    random.Random(seed).shuffle(rows)
    rows = rows[:sample]
    calls = []
    for r in rows:
        near = neighbors(store, _parse_vec(r["f"]), series, rule, 0.0, k, before=cut)
        est = estimate(near)
        go, _why = decide(est, min_n, z)
        calls.append((go, est["mean"], est["mean"] - z * est["se"] if math.isfinite(est["se"]) else float("-inf"), float(r["roi"]), r["ticker"], r["side"]))
    def stats(sub: list[tuple]) -> tuple[int, float, float]:
        if not sub:
            return 0, 0.0, 0.0
        by: dict[tuple[str, str], list[float]] = defaultdict(list)
        for c in sub:
            by[(c[4], c[5])].append(c[3])
        means = [sum(v) / len(v) for v in by.values()]
        m = sum(means) / len(means)
        se = math.sqrt(sum((x - m) ** 2 for x in means) / (len(means) - 1) / len(means)) if len(means) > 1 else float("nan")
        return len(sub), sum(c[3] for c in sub) / len(sub), se
    buys = [c for c in calls if c[0]]
    by_mean = sorted(calls, key=lambda c: c[1])
    deciles = [by_mean[i * len(by_mean) // 5:(i + 1) * len(by_mean) // 5] for i in range(5)]
    return {
        "cut": cut, "test_entries": len(calls), "all": stats(calls), "buy": stats(buys),
        "top_fifth": stats(deciles[-1]), "bottom_fifth": stats(deciles[0]),
        "drop_worst_fifth": stats([c for d in deciles[1:] for c in d]), "drop_worst_two_fifths": stats([c for d in deciles[2:] for c in d]),
        "by_fifth": [(sum(c[1] for c in d) / len(d), stats(d)[1]) for d in deciles if d],
    }
