"""The case memory of winning behavior: what a market looked like at a moment, and what became of it.

Every finished 15-minute crypto window is replayed from the tape. At each sampled moment (every 30 s, from minute 3 to two minutes before the close) where a side is priced 70-92c with a spread of
8c or less, the full feature snapshot (factors.snapshot_full) is saved with the outcome that followed: did the side win, did its bid touch 99c, how low did the bid go afterwards, and how many minutes
until it touched 99c. Our own real buys are saved too (kind 'buy'), with the snapshot taken at the fill. The table outlives the tape's 14 days, so studies and the forecaster's memory can use the full history.

    python -m kalshi_moonshot behavior-cases     add the windows finished since the last run
    python -m kalshi_moonshot behavior-report    which features separate the markets that reach 99c from those that do not

Nothing here places an order.
"""

from __future__ import annotations

import bisect
import json
import logging
import os
import statistics
import subprocess
import sys
import threading
import time
from typing import Any

from . import db, factors, forecast
from .config import Settings
from .forecast_fit import CRYPTO
from .store import Store

log = logging.getLogger("moonshot")

STEP = 30.0
FIRST_MINUTE = 3.0
LAST_LEFT = 120.0
FX = ("KXEURUSD15M", "KXGBPUSD15M", "KXUSDJPY15M", "KXAUDUSD15M", "KXUSDCAD15M")
COMMODITY = ("KXGOLD15M", "KXSILVER15M", "KXWTI15M", "KXBRENT15M", "KXCOPPER15M", "KXNATGAS15M", "KXPLATINUM15M", "KXPALLADIUM15M")
ALL = CRYPTO + FX + COMMODITY  # every instrument on its own: a category is never one row, each series is saved and reported by name
BAND = (0.70, 0.92)
MAX_SPREAD = 0.08


def _weights(s: Settings) -> forecast.Weights | None:
    try:
        return forecast.load_weights(s.forecast_weights or f"{s.data_dir}/forecast_h5.json")
    except Exception:
        return None  # without weights the slices and the forecast are left out; the rest is still saved


def build_cases(s: Settings, max_windows: int = 400) -> int:
    """Add the finished windows that have no moments saved yet. Returns how many moments were added."""
    tape = db.connect(s.tape_path, "tape", shared=True)
    store = Store(s.db_path, s.exclude_versions)
    weights = _weights(s)
    done = {r[0] for r in store.db.execute("SELECT DISTINCT ticker FROM behavior_cases WHERE kind='moment'").fetchall()}
    marks = ",".join("?" * len(ALL))
    start = tape.execute("SELECT MIN(ts) FROM quotes").fetchone()[0] or 0.0  # windows that closed before the tape began have nothing to replay
    windows = [w for w in store.db.execute(f"SELECT ticker, series, result, close_ts, strike FROM windows WHERE series IN ({marks}) AND result IN ('yes','no') AND close_ts > ? ORDER BY close_ts DESC", (*ALL, start)).fetchall()
               if w["ticker"] not in done]
    added = 0
    for w in windows[:max_windows]:
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (w["ticker"],)).fetchall()
        close = float(w["close_ts"])
        if len(q) < 60:
            store.db.execute("INSERT OR IGNORE INTO behavior_cases (ticker, side, ts, kind, series, close_ts, features) VALUES (?,?,?,?,?,?,?)", (w["ticker"], "-", close, "moment", w["series"], close, "{}"))  # nothing recorded: do not look again
            continue
        qts = [r[0] for r in q]
        for side in ("yes", "no"):
            bids = [r[1] if side == "yes" else 1 - r[2] for r in q]
            asks = [r[2] if side == "yes" else 1 - r[1] for r in q]
            t = close - 900.0 + FIRST_MINUTE * 60.0
            while t < close - LAST_LEFT:
                i = bisect.bisect_right(qts, t) - 1
                t += STEP
                if i < 0 or not (BAND[0] <= asks[i] <= BAND[1]) or asks[i] - bids[i] > MAX_SPREAD:
                    continue
                snap = factors.snapshot_full(tape, w["ticker"], side, close, qts[i], w["strike"], weights)
                if not snap:
                    continue
                after = bids[i:]
                reach = next((k for k, b in enumerate(after) if b >= 0.99), None)
                store.db.execute("INSERT OR IGNORE INTO behavior_cases (ticker, side, ts, kind, series, close_ts, features, won, reach99, low_bid, minutes_to_99) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                 (w["ticker"], side, qts[i], "moment", w["series"], close, json.dumps(snap), int(w["result"] == side), int(reach is not None), min(after),
                                  None if reach is None else (qts[i + reach] - qts[i]) / 60.0))
                added += 1
    for r in store.db.execute("SELECT ts, ticker, side, factors, outcome FROM decisions WHERE action='bet' AND mode='trade' AND outcome IN ('yes','no') AND factors IS NOT NULL AND factors LIKE '{%'").fetchall():
        close = tape.execute("SELECT MAX(ts) FROM quotes WHERE ticker=?", (r["ticker"],)).fetchone()[0]
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>=? AND bid > 0 AND ask > 0 ORDER BY ts", (r["ticker"], r["ts"])).fetchall()
        if not q or close is None:
            continue
        bids = [x[1] if r["side"] == "yes" else 1 - x[2] for x in q]
        reach = next((k for k, b in enumerate(bids) if b >= 0.99), None)
        cur = store.db.execute("INSERT OR IGNORE INTO behavior_cases (ticker, side, ts, kind, series, close_ts, features, won, reach99, low_bid, minutes_to_99) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (r["ticker"], r["side"], r["ts"], "buy", r["ticker"].split("-")[0], close, r["factors"], int(r["outcome"] == r["side"]), int(reach is not None), min(bids),
                                None if reach is None else (q[reach][0] - q[0][0]) / 60.0))
        added += max(0, cur.rowcount or 0)
    store.db.commit()
    log.info("behavior: %d new moments saved", added)
    try:
        from . import vectors

        vectors.build(s)  # the same finished windows, as vector points at every scale
    except Exception:
        log.exception("behavior: could not save the vector points")
    try:
        gates = write_gates(s)
        log.info("behavior: %d skip rules from %d windows", len(gates["gates"]), gates["windows"])
    except Exception:
        log.exception("behavior: could not write the skip rules")
    return added


GATE_FEATURES = ("slice_7_15", "prior_15", "held_min")  # the ones the walk-forward test found worse at one end on windows they had not seen
GATE_MIN_WINDOWS = 100


def write_gates(s: Settings, path: str = "") -> dict[str, Any]:
    """For each gate feature: which end of its recorded range earns less edge beyond the price (win minus ask), and where the worst quarter starts. Written for the forecast estimator to read."""
    store = Store(s.db_path, s.exclude_versions)
    rows = store.db.execute("SELECT ticker, features, won FROM behavior_cases WHERE kind='moment' AND side != '-' AND features != '{}'").fetchall()
    cases = []
    for r in rows:
        f = _flat(json.loads(r["features"]))
        if f.get("ask"):
            cases.append((f, r["won"] - f["ask"], r["ticker"]))
    windows = len({c[2] for c in cases})
    out: dict[str, Any] = {"made_at": time.time(), "windows": windows, "gates": {}}
    if windows >= GATE_MIN_WINDOWS:
        for name in GATE_FEATURES:
            pairs = sorted(((f[name], e) for f, e, _ in cases if name in f), key=lambda p: p[0])
            q = len(pairs) // 4
            if q < 50:
                continue
            low, high = statistics.mean(e for _, e in pairs[:q]), statistics.mean(e for _, e in pairs[-q:])
            worse, cutoff, bad, rest = ("low", pairs[q][0], low, high) if low < high else ("high", pairs[-q][0], high, low)
            out["gates"][name] = {"worse": worse, "cutoff": cutoff, "edge_in_worse_quarter": round(bad, 4), "edge_at_the_other_end": round(rest, 4)}
    path = path or os.path.join(s.data_dir, "forecast_gates.json")
    with open(f"{path}.tmp", "w") as fh:
        json.dump(out, fh)
    os.replace(f"{path}.tmp", path)
    return out


def _flat(features: dict[str, Any]) -> dict[str, float]:
    out = {k: float(v) for k, v in features.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    for n, z in enumerate(features.get("slices") or []):
        out[f"slice_{forecast.EDGES[n]}_{forecast.EDGES[n + 1]}"] = float(z)
    return out


def report_by_series(s: Settings, min_windows: int = 30) -> list[str]:
    """Each instrument on its own: how often its 70-92c favorites won, the fee-free return of buying them at the ask, and the three features that separate its winners from its losers most (top quartile minus bottom quartile)."""
    store = Store(s.db_path, s.exclude_versions)
    rows = store.db.execute("SELECT series, ticker, features, won FROM behavior_cases WHERE kind='moment' AND side != '-' AND features != '{}'").fetchall()
    by: dict[str, list[tuple[dict[str, float], Any]]] = {}
    for r in rows:
        by.setdefault(r["series"], []).append((_flat(json.loads(r["features"])), r))
    out = ["per instrument (fee-free; features measured as top quartile minus bottom quartile of the win rate)"]
    for series in sorted(by):
        cases = by[series]
        windows = len({r["ticker"] for _, r in cases})
        asks = [f.get("ask") for f, _ in cases if f.get("ask")]
        if windows < min_windows or not asks:
            out.append(f"  {series:14} {windows:4} windows: too few")
            continue
        gross = statistics.mean((r["won"] - f["ask"]) / f["ask"] for f, r in cases if f.get("ask"))
        diffs = []
        for name in sorted({k for f, _ in cases for k in f} - {"ask", "bid", "strike"}):
            pairs = sorted(((f[name], r["won"]) for f, r in cases if name in f), key=lambda p: p[0])
            q = len(pairs) // 4
            if q >= 30:
                diffs.append((statistics.mean(w for _, w in pairs[-q:]) - statistics.mean(w for _, w in pairs[:q]), name))
        top = ", ".join(f"{n} {100 * d:+.0f}" for d, n in sorted(diffs, key=lambda x: -abs(x[0]))[:3])
        out.append(f"  {series:14} {windows:4} windows, {len(cases):5} moments: won {statistics.mean(r['won'] for _, r in cases):4.0%}, return {gross:+6.1%}; strongest: {top}")
    return out


def report(s: Settings, min_windows: int = 40) -> list[str]:
    """Quartiles of each feature against the chance of winning and of touching 99c, from the saved moments."""
    store = Store(s.db_path, s.exclude_versions)
    rows = store.db.execute("SELECT ticker, features, won, reach99, minutes_to_99 FROM behavior_cases WHERE kind='moment' AND side != '-' AND features != '{}'").fetchall()
    cases = [(dict(_flat(json.loads(r["features"])).items()), r) for r in rows]
    windows = len({r["ticker"] for _, r in cases})
    out = [f"{len(cases):,} saved moments from {windows} windows; won {statistics.mean(r['won'] for _, r in cases):.1%}, touched 99c {statistics.mean(r['reach99'] for _, r in cases):.1%}" if cases else "nothing saved yet"]
    if windows < min_windows:
        return [*out, f"  fewer than {min_windows} windows: too few to rank features"]
    names = sorted({k for f, _ in cases for k in f})
    table = []
    for k in names:
        pairs = sorted(((f[k], r) for f, r in cases if k in f), key=lambda p: p[0])
        if len(pairs) < 200:
            continue
        q = len(pairs) // 4
        lo, hi = [r for _, r in pairs[:q]], [r for _, r in pairs[-q:]]
        table.append((k, statistics.mean(r["won"] for r in hi) - statistics.mean(r["won"] for r in lo), statistics.mean(r["won"] for r in lo), statistics.mean(r["won"] for r in hi),
                      statistics.mean(r["reach99"] for r in hi) - statistics.mean(r["reach99"] for r in lo), pairs[q][0], pairs[-q][0]))
    out.append("  feature             top quartile minus bottom quartile: win rate | touched 99c   (win rates bottom -> top)   quartile edges")
    for k, d, lo, hi, dr, a, b in sorted(table, key=lambda x: -abs(x[1])):
        out.append(f"  {k:19} {100 * d:+6.1f} pts | {100 * dr:+6.1f} pts   ({lo:5.1%} -> {hi:5.1%})   {a:+.3g} to {b:+.3g}")
    return out


def start_cases(get_settings: Any) -> threading.Thread:
    """A thread that adds the newly finished windows every `behavior_cases_hours` (0 = never), in a separate low-priority process so the recorder and the watchers are not slowed."""

    def run() -> None:
        time.sleep(300.0)
        while True:
            s = get_settings()
            try:
                if s.behavior_cases_hours > 0:
                    done = subprocess.run([sys.executable, "-m", "kalshi_moonshot", "behavior-cases"], capture_output=True, text=True, timeout=3600, preexec_fn=lambda: os.nice(10))
                    if done.returncode:
                        log.warning("behavior cases failed: %s", (done.stderr or done.stdout)[-300:])
            except Exception:
                log.exception("behavior cases could not run")
            time.sleep(max(600.0, (s.behavior_cases_hours or 2.0) * 3600.0))

    th = threading.Thread(target=run, name="behavior-cases", daemon=True)
    th.start()
    return th

