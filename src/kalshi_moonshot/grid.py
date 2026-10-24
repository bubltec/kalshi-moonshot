"""Learn when and at what price to buy, from every recorded 15-minute window. The exit is the one the bot already uses.

For each settled window with ticks on the tape, every entry that was on offer is replayed: the first time the side's ask sat in each price band
inside each minute band of the window, then managed with the live peak-crash exit (armed once the bid held 10% over the price paid, sold 4 cents
off a held peak for 10 seconds or 12 cents at once, otherwise held to settlement). Each replay is one observation of (minute band X, price band Y),
and it is kept in `grid_obs` so the record outlives the tape. Two more facts ride on each observation, both measured the same way live and in
history from the price feeds the tape records:

  alpha  the instrument's own move over the last hour, in its own typical hourly moves, as it bears on the side bought
  beta   the same move averaged across every instrument we follow (the whole market)
  edge   how far the ask sits from the fair value implied by where the price is against the strike and how long is left: "cheap" (the ask is
         8+ cents under fair), "dear" (8+ over) or "fair". This is the "price right at the target but the market still quotes 29%" case.

alpha and beta are each "with" the side bought (the side profits if the move continues), "against" it, or "flat".

A lane with `grid = true` buys only where a cell has enough finished replays and its lower confidence bound is above zero, using the most
specific cell that has the evidence (X, Y plus any of edge, alpha, beta, most specific first, down to X, Y alone). Where none clears, it passes. Choosing the best of many
cells flatters the winner, so the bar is a strict lower bound (`grid_z`, 3 standard errors by default: with about 300 cells, a few clear 2 by chance alone).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db, gate
from .strategy import fee_per_contract as fee

log = logging.getLogger("moonshot")

MBANDS = gate.BANDS  # minutes into the window: 0-3, 3-6, 6-9, 9-12, 12-15
PBANDS = tuple((round(0.10 + 0.10 * i, 2), round(0.20 + 0.10 * i, 2)) for i in range(8))  # ask 0.10-0.20 ... 0.80-0.90
CUSHION = 0.01  # a sale is a cent under the bid that triggered it
END_MARGIN = 5.0  # seconds before the close that a replay stops
# the live peak-crash exit (the lanes' trail_* settings): armed at +ACT on a peak that held HOLD seconds; sold DROP under it for CONFIRM seconds, or FAST at once
ACT, DROP, FAST, CONFIRM, HOLD = 0.10, 0.04, 0.12, 10.0, 3.0
HOUR = 3600.0
FLAT = 0.5  # a move under this many typical hourly moves is "flat"
EDGE = 0.08  # a side priced this far under (over) its fair value is "cheap" ("dear")
GRID_VERSION = 4  # bump when the replay changes: the stored replays are then rebuilt from the tape
UPDATE_EVERY = 600.0

# where each 15-minute series' price comes from on the tape: ("index", id) in index_ticks or ("feed", symbol) in feed_ticks
SOURCES: dict[str, tuple[str, str]] = {
    "KXEURUSD15M": ("feed", "hl:xyz:EUR"), "KXGBPUSD15M": ("feed", "hl:xyz:GBP"), "KXUSDJPY15M": ("feed", "hl:xyz:JPY"),
    "KXBTC15M": ("index", "BRTI"), "KXETH15M": ("index", "ETHUSD_RTI"), "KXSOL15M": ("index", "SOLUSD_RTI"),
    "KXXRP15M": ("index", "XRPUSD_RTI"), "KXDOGE15M": ("index", "DOGEUSD_RTI"),
    "KXGOLD15M": ("feed", "hl:xyz:GOLD"), "KXSILVER15M": ("feed", "hl:xyz:SILVER"), "KXWTI15M": ("feed", "hl:xyz:CL"),
    "KXNATGAS15M": ("feed", "hl:xyz:NATGAS"), "KXCOPPER15M": ("feed", "hl:xyz:COPPER"),
    "KXPLATINUM15M": ("feed", "hl:xyz:PLATINUM"), "KXPALLADIUM15M": ("feed", "hl:xyz:PALLADIUM"),
}

# the whole market, for beta: commodities and crypto (the currency pairs have a feed for their own alpha and price, but one US dollar move would swamp the basket)
BASKET = tuple(s for s in SOURCES if not any(c in s for c in ("EUR", "GBP", "JPY")))


@dataclass(frozen=True)
class Cell:
    n: int
    mean: float
    se: float

    def lower(self, z: float) -> float:
        return self.mean - z * self.se


def pband_of(ask: float) -> int | None:
    for i, (lo, hi) in enumerate(PBANDS):
        if lo <= ask < hi:
            return i
    return None


def mband_of(minutes_in: float) -> int | None:
    band = gate.band_of(minutes_in)
    return MBANDS.index(band) if band is not None else None


def bucket(move: float, side: str) -> str:
    """A signed move in typical hourly moves, as it bears on the side bought: YES profits from a rise, NO from a fall."""
    aligned = move if side == "yes" else -move
    return "with" if aligned > FLAT else "against" if aligned < -FLAT else "flat"


# ---- price features, from the tape ------------------------------------------------------------------------------------------------

def _px_at(tape: Any, src: tuple[str, str], ts: float) -> float | None:
    table, col, val = ("index_ticks", "index_id", "value") if src[0] == "index" else ("feed_ticks", "symbol", "px")
    row = tape.execute(f"SELECT {val} FROM {table} WHERE {col}=? AND ts<=? AND ts>? ORDER BY ts DESC LIMIT 1", (src[1], ts, ts - 120.0)).fetchone()
    return float(row[0]) if row and row[0] else None


class Features:
    """Hour moves per instrument, scaled by each one's own typical hourly move, and their market-wide average."""

    def __init__(self, tape: Any):
        self.tape = tape
        self.sigma: dict[str, float] = {}
        self._cache: dict[tuple[str, int], float | None] = {}

    def _scale(self, series: str, lag: float = HOUR) -> float | None:
        key = series if lag == HOUR else f"{series}@{lag:g}"
        if key not in self.sigma:
            src = SOURCES.get(series)
            lo_hi = None
            if src:
                table, col = ("index_ticks", "index_id") if src[0] == "index" else ("feed_ticks", "symbol")
                lo_hi = self.tape.execute(f"SELECT MIN(ts), MAX(ts) FROM {table} WHERE {col}=?", (src[1],)).fetchone()
            rets: list[float] = []
            if src and lo_hi and lo_hi[0] and lo_hi[1]:
                t = lo_hi[0] + lag + 120
                while t <= lo_hi[1]:
                    a, b = _px_at(self.tape, src, t), _px_at(self.tape, src, t - lag)
                    if a and b:
                        rets.append(math.log(a / b))
                    t += 900.0
            self.sigma[key] = (math.sqrt(sum(r * r for r in rets) / len(rets)) if len(rets) >= 20 else 0.0)
        return self.sigma[key] or None

    def alpha(self, series: str, ts: float) -> float | None:
        key = (series, int(ts // 60))
        if key not in self._cache:
            src, sig = SOURCES.get(series), self._scale(series)
            now, then = (_px_at(self.tape, src, ts), _px_at(self.tape, src, ts - HOUR)) if src else (None, None)
            self._cache[key] = math.log(now / then) / sig if (now and then and sig) else None
        return self._cache[key]

    def gap(self, series: str, ts: float, strike: float | None, left_minutes: float, ask: float, side: str) -> float | None:
        """Fair value of the side (a random walk from the spot to the strike over the time left) minus its ask, in dollars; positive = cheap."""
        src, sig = SOURCES.get(series), self._scale(series, 900.0)
        spot = _px_at(self.tape, src, ts) if src else None
        if not (spot and strike and sig and strike > 0):
            return None
        sd = sig * math.sqrt(max(left_minutes, 0.25) / 15.0)
        p_yes = 0.5 * (1.0 + math.erf(math.log(spot / strike) / (sd * math.sqrt(2.0))))
        return (p_yes if side == "yes" else 1.0 - p_yes) - ask

    def edge(self, series: str, ts: float, strike: float | None, left_minutes: float, ask: float, side: str) -> str | None:
        """cheap / fair / dear: the side's ask against the fair value a random walk gives from the spot, the strike and the time left."""
        gap = self.gap(series, ts, strike, left_minutes, ask, side)
        if gap is None:
            return None
        return "cheap" if gap >= EDGE else "dear" if gap <= -EDGE else "fair"

    def beta(self, ts: float) -> float | None:
        moves = [m for s in BASKET if (m := self.alpha(s, ts)) is not None]
        return sum(moves) / len(moves) if len(moves) >= 3 else None


def _open_tape(path: Path) -> Any:
    if not db.tape_exists(path):
        return None
    return db.connect(path, "tape", shared=True)  # this module only ever reads the tape


# ---- replay ---------------------------------------------------------------------------------------------------------------------

def _roi_exit(entry: float, bid: float) -> float:
    sale = bid - CUSHION
    return (sale - fee(sale)) / (entry + fee(entry)) - 1.0


def trail_exit(path: list[tuple[float, float, float]], entry: float) -> float | None:
    """The bid at which the live peak-crash exit would sell, or None if it never does (the position is held to settlement)."""
    peak, below = entry, None
    last, since = None, 0.0  # the current bid and when it first showed; a repeated quote at the same bid does not restart the clock
    for t, bid, _ask in path:
        if bid != last:
            if last is not None and t - since >= HOLD and last > peak:
                peak = last
            last, since = bid, t
        elif t - since >= HOLD and bid > peak:
            peak = bid
        if not (peak >= entry * (1 + ACT) - 1e-9 and peak > entry):
            below = None
            continue
        if bid <= peak - FAST + 1e-9:
            return bid
        if bid <= peak - DROP + 1e-9:
            below = t if below is None else below
            if t - below >= CONFIRM:
                return bid
        else:
            below = None
    return None


def replay(rows: list[tuple[float, float, float]], close: float, side: str, result: str, rule: str = "trail") -> list[tuple[int, int, float, float, float]]:
    """One window, one side: (minute band, price band, roi, entry ts, entry ask) for the first entry in each cell.

    rule "trail" manages each entry with the live peak-crash exit; "hold" keeps it to settlement (what a lane with no exit rules does)."""
    start = close - 900.0
    path = [(t, b if side == "yes" else 1.0 - a, a if side == "yes" else 1.0 - b) for t, b, a in rows if start <= t < close - END_MARGIN]
    out: list[tuple[int, int, float, float, float]] = []
    seen: set[tuple[int, int]] = set()
    for i, (t, _bid, ask) in enumerate(path):
        mb, pb = mband_of((t - start) / 60.0), pband_of(ask)
        if mb is None or pb is None or (mb, pb) in seen or ask <= 0:
            continue
        seen.add((mb, pb))
        sold = trail_exit(path[i + 1:], ask) if rule == "trail" else None
        roi = _roi_exit(ask, sold) if sold is not None else (1.0 if result == side else 0.0) / (ask + fee(ask)) - 1.0
        out.append((mb, pb, roi, t, ask))
    return out


def update(store: Any, tape_path: Path, series: tuple[str, ...], now: float | None = None, rule: str = "trail") -> int:
    """Replay every settled window not yet replayed under this exit rule. Returns the number of windows added."""
    now = now or time.time()
    tape = _open_tape(tape_path)
    if tape is None:
        return 0
    feats = Features(tape)
    have = store.db.execute("SELECT v FROM grid_meta WHERE k='version'").fetchone()
    if not have or int(have["v"]) != GRID_VERSION:  # the replay changed since these were stored: start the record again from the tape
        store.db.execute("DELETE FROM grid_obs")
        store.db.execute("DELETE FROM grid_done")
        store.db.execute("INSERT OR REPLACE INTO grid_meta (k, v) VALUES ('version', ?)", (GRID_VERSION,))
        store.db.commit()
    marks = ",".join("?" * len(series))
    todo = store.db.execute(
        f"SELECT w.ticker, w.series, w.close_ts, w.result, w.strike FROM windows w LEFT JOIN grid_done d ON d.ticker=w.ticker AND d.rule=? "
        f"WHERE d.ticker IS NULL AND w.result IN ('yes','no') AND w.series IN ({marks}) AND w.close_ts < ? ORDER BY w.close_ts",
        (rule, *series, now - 1200.0),
    ).fetchall()
    added = 0
    for w in todo:
        rows = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts", (w["ticker"],)).fetchall()
        close = float(w["close_ts"])
        if rows and rows[0][0] <= close - 900.0 + 120.0:  # the tape saw the window from near its open
            for side in ("yes", "no"):
                for mb, pb, roi, ts, ask in replay([(r[0], r[1], r[2]) for r in rows], close, side, w["result"], rule):
                    a, b = feats.alpha(w["series"], ts), feats.beta(ts)
                    e = feats.edge(w["series"], ts, w["strike"], (close - ts) / 60.0, ask, side)
                    store.db.execute(
                        "INSERT OR REPLACE INTO grid_obs (ticker, series, side, mband, pband, rule, roi, close_ts, alpha, beta, edge) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (w["ticker"], w["series"], side, mb, pb, rule, roi, int(close),
                         None if a is None else bucket(a, side), None if b is None else bucket(b, side), e),
                    )
            added += 1
        store.db.execute("INSERT OR IGNORE INTO grid_done (ticker, rule) VALUES (?, ?)", (w["ticker"], rule))
    store.db.commit()
    tape.close()
    return added


_lock = threading.Lock()
_last: dict[tuple, float] = {}


def refresh_soon(store_factory: Any, tape_path: Path, series: tuple[str, ...], rule: str = "trail") -> None:
    """Bring the record up to date in the background, at most every UPDATE_EVERY seconds for each series and exit rule."""
    with _lock:
        if time.time() - _last.get((series, rule), 0.0) < UPDATE_EVERY:
            return
        _last[(series, rule)] = time.time()

    def work() -> None:
        try:
            n = update(store_factory(), tape_path, series, rule=rule)
            if n:
                log.info("grid: replayed %d more windows", n)
        except Exception:
            log.exception("grid: update failed")

    threading.Thread(target=work, name="grid-update", daemon=True).start()


# ---- the record -----------------------------------------------------------------------------------------------------------------

def _cell(n: int, avg: float | None, sq: float | None) -> Cell:
    if not n or avg is None:
        return Cell(0, 0.0, float("inf"))
    var = max(0.0, (sq or 0.0) / n - avg * avg) * n / (n - 1) if n > 1 else float("inf")
    return Cell(n, avg, math.sqrt(var / n) if n > 1 else float("inf"))


# which of (edge, alpha, beta) a level splits on, from the most specific down to none; a cell uses the first level that has the evidence
LEVELS = ((1, 1, 1), (1, 1, 0), (0, 1, 1), (1, 0, 0), (0, 1, 0), (0, 0, 0))
LABELS = {(1, 1, 1): "price+hour+market", (1, 1, 0): "price+hour", (0, 1, 1): "hour+market", (1, 0, 0): "price", (0, 1, 0): "hour", (0, 0, 0): "base"}


def cells(store: Any, series: tuple[str, ...], since: float = 0.0, rule: str = "trail") -> dict[tuple, Cell]:
    """Cells keyed (mband, pband, edge, alpha, beta), with None where a level does not split on that feature."""
    marks = ",".join("?" * len(series))
    out: dict[tuple, Cell] = {}
    for use_e, use_a, use_b in LEVELS:
        cols = [c for c, on in (("edge", use_e), ("alpha", use_a), ("beta", use_b)) if on]
        extra = "".join(f", {c}" for c in cols)
        need = "".join(f" AND {c} IS NOT NULL" for c in cols)
        sql = (f"SELECT mband, pband{extra}, COUNT(*) n, AVG(roi) m, SUM(roi*roi) sq FROM grid_obs "
               f"WHERE series IN ({marks}) AND close_ts >= ? AND rule = ?{need} GROUP BY mband, pband{extra}")
        for r in store.db.execute(sql, (*series, since, rule)):
            out[(r["mband"], r["pband"], r["edge"] if use_e else None, r["alpha"] if use_a else None, r["beta"] if use_b else None)] = _cell(r["n"], r["m"], r["sq"])
    return out


def best_cell(table: dict[tuple, Cell], mb: int, pb: int, e: str | None, a: str | None, b: str | None, min_n: int) -> tuple[Cell, str] | None:
    """The most specific cell for these conditions that has `min_n` replays."""
    have = {"e": e, "a": a, "b": b}
    for level in LEVELS:
        feats = tuple(have[k] if on else None for k, on in zip("eab", level, strict=True))
        if any(on and have[k] is None for k, on in zip("eab", level, strict=True)):
            continue  # a feature we cannot measure right now
        c = table.get((mb, pb, *feats))
        if c and c.n >= min_n:
            return c, LABELS[level]
    return None


def pick(store: Any, spec: Any, series: tuple[str, ...], elapsed: float, ask: float, edge: str | None, alpha: str | None, beta: str | None,
         since: float = 0.0) -> tuple[bool, str]:
    """Whether to buy here, with why. Needs a cell with `grid_min_n` replays whose mean clears zero by `grid_z` standard errors."""
    mb, pb = mband_of(elapsed), pband_of(ask)
    if mb is None or pb is None:
        return False, f"grid: minute {elapsed:.1f} or price {ask:.2f} is outside the grid"
    where = f"minutes {gate.BANDS[mb][0]}-{gate.BANDS[mb][1]}, ask {PBANDS[pb][0]:.2f}-{PBANDS[pb][1]:.2f}"
    table = cells(store, series, since, getattr(spec, "grid_rule", "trail"))
    found = best_cell(table, mb, pb, edge, alpha, beta, spec.grid_min_n)
    if found is None:
        have = table.get((mb, pb, None, None, None), Cell(0, 0, 0)).n
        return False, f"grid: only {have} replays so far for {where}; needs {spec.grid_min_n}"
    c, label = found
    lcb = c.lower(spec.grid_z)
    says = f"{c.mean:+.1%} over {c.n} replays ({label} cell, {where}), {spec.grid_z:g} standard errors under is {lcb:+.1%}"
    return (lcb > 0), ("grid: buy, " if lcb > 0 else "grid: pass, ") + says


def live_features(tape_path: Path, series: str, side: str, ts: float, strike: float | None, left_minutes: float, ask: float) -> tuple[str | None, str | None, str | None]:
    """The (edge, alpha, beta) buckets for a bet about to be made now."""
    tape = _open_tape(tape_path)
    if tape is None:
        return None, None, None
    try:
        f = Features(tape)
        a, b = f.alpha(series, ts), f.beta(ts)
        return f.edge(series, ts, strike, left_minutes, ask, side), (None if a is None else bucket(a, side)), (None if b is None else bucket(b, side))
    finally:
        tape.close()
