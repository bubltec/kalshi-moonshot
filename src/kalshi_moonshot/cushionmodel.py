"""The chance a side holds, from Kalshi's own price moved by the spot's cushion, fitted on the tape.

2026-11-16, user: "it should matter what kind of cushion it has. it is fine to buy flat at a high prediction (> 70) and less than 3 mins left"; "how do we translate that to logic without hard limits. needs to be based on the spot cushion and time remaining"; "the more time remaining the more cushion it needs"; "kalshi already does the prediction, we just augment it with our own historical view"; "Go live with it".

The cushion: how far the market's Kalshi perp sits on the side's side of its level at the market's open, in units of what the perp typically moves in the time left (its root-mean-square 90 s move over the last 3 hours, x sqrt(time left / 90)). More time left makes the same distance a smaller cushion.
The chance: logistic(w1 x logit(Kalshi's mid) + w2 x cushion), one pair of weights per stretch of time left, fitted on the last days of the tape and rebuilt daily (its own low-priority process, as staterecord does). No price band, no minute band, no coin.
studies/results/2026-11-16-cushion-calibration.txt, walk-forward on four held-out days: by the chance less the ask and its fee: under -0.10, -16.7%; within 0.03, -4.8%; +0.03 to +0.06, +5.0% (1,849 markets); +0.06 to +0.10, +6.0%; +0.10 and over, +31.5%. With 1 to 6 minutes left every edge from +0.03 pays; with 9 to 14 minutes left it takes +0.06 to +0.10. The cushion alone, rescaled, did not do this (it is a far worse predictor than the price).

    python -m kalshi_moonshot.cushionmodel        # rebuild data/cushion_model.json from the tape
"""
from __future__ import annotations

import bisect
import json
import logging
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

LOOK = 90.0  # seconds: the stretch the typical move is read over
STRETCHES = 120  # back-to-back stretches before now: 3 hours
BANDS = ((60.0, 180.0), (180.0, 360.0), (360.0, 540.0), (540.0, 720.0), (720.0, 841.0))  # seconds left; structural stretches of a 15-minute market, each with its own weights
MIN_ROWS = 2000  # moments a band needs before it is fitted
FIT_DAYS = 5.0  # the tape the fit reads: the last days, so the weights follow the market
REFIT_EVERY = 24 * 3600.0
EDGE_REF = 180.0  # the lane's edge is asked in full at 3 minutes left and grows with sqrt(time left / 3 minutes)

_cache: dict[str, Any] = {"data": None, "mtime": 0.0}
_fitting: subprocess.Popen | None = None


def cushion(ts: list[float], vs: list[float], now: float, ref: float, side: str, left: float) -> float | None:
    """The side's cushion in typical moves for the time left, + = the perp is on the side's side of `ref` (its level at the market's open). None without a tick within two minutes of every 90 s mark of the last 3 hours, with a flat 3 hours, or with no reference or no time left."""
    if not ts or len(ts) != len(vs) or ref <= 0 or left <= 0:
        return None
    levels: list[float] = []
    for n in range(STRETCHES + 2):
        at = now - LOOK * n
        i = bisect.bisect_right(ts, at) - 1
        if i < 0 or at - ts[i] > 120.0 or vs[i] <= 0:
            return None
        levels.append(math.log(vs[i]))
    past = [levels[n] - levels[n + 1] for n in range(1, STRETCHES + 1)]
    rms = math.sqrt(sum(x * x for x in past) / len(past))
    if rms <= 0:
        return None
    return (1.0 if side == "yes" else -1.0) * (levels[0] - math.log(ref)) / (rms * math.sqrt(left / LOOK))


def weights(model: dict[str, Any] | None, left: float) -> tuple[float, float] | None:
    """(w1, w2) for this much time left, or None when the model has no fit for that stretch."""
    for b in (model or {}).get("bands", ()):
        if b["lo"] <= left < b["hi"]:
            return float(b["w1"]), float(b["w2"])
    return None


def chance(model: dict[str, Any] | None, mid: float, cush: float, left: float) -> float | None:
    """The chance the side holds: Kalshi's mid for the side, moved by its cushion. The model has no constant, so the two sides of a market add to 1."""
    w = weights(model, left)
    if w is None or not (0.0 < mid < 1.0):
        return None
    m = min(0.995, max(0.005, mid))
    x = w[0] * math.log(m / (1.0 - m)) + w[1] * cush
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, x))))


def edge_needed(base: float, left: float) -> float:
    """The edge over the price the lane asks for (lane field entry_cushion_edge_min = `base`): `base` at 3 minutes left, growing with sqrt(time left): 0.03 asks 0.024 at 2 minutes, 0.047 at 7.5 and 0.062 at 13. The held-out table needed more with more time left (+0.03 paid with 1 to 6 minutes left, +0.06 to +0.10 with 9 to 14); the square root is the same clock the cushion itself uses. Read off that table after seeing it."""
    return base * math.sqrt(max(left, 0.0) / EDGE_REF)


def load(s: Any) -> dict[str, Any] | None:
    p = Path(s.data_dir) / "cushion_model.json"
    try:
        m = p.stat().st_mtime
        if _cache["data"] is None or m != _cache["mtime"]:
            _cache["data"], _cache["mtime"] = json.loads(p.read_text()), m
    except (OSError, ValueError):
        return _cache["data"]
    return _cache["data"]


def maybe_refit(s: Any, now: float | None = None) -> bool:
    """Start the daily rebuild as its own low-priority process. True when one was started."""
    global _fitting
    now = time.time() if now is None else now
    if _fitting is not None and _fitting.poll() is None:
        return False
    model = load(s)
    if model and now - model["built_at"] < REFIT_EVERY:
        return False
    log.info("cushion model: rebuilding from the tape (separate process)")
    _fitting = subprocess.Popen([sys.executable, "-m", "kalshi_moonshot.cushionmodel"], preexec_fn=lambda: os.nice(15))  # lowers the child's priority only
    return True


def fit(rows: list[tuple[float, float, bool]]) -> tuple[float, float]:
    """(w1, w2) of P(yes) = logistic(w1 x logit(mid) + w2 x cushion) by Newton's method; `rows` are (yes mid, yes cushion, yes won)."""
    w1, w2 = 1.0, 0.0
    for _ in range(25):
        g1 = g2 = h11 = h12 = h22 = 0.0
        for mid, c, won in rows:
            m = min(0.995, max(0.005, mid))
            x1 = math.log(m / (1.0 - m))
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, w1 * x1 + w2 * c))))
            e, v = (1.0 if won else 0.0) - p, p * (1.0 - p)
            g1 += e * x1
            g2 += e * c
            h11 += v * x1 * x1
            h12 += v * x1 * c
            h22 += v * c * c
        det = h11 * h22 - h12 * h12
        if det <= 1e-12:
            break
        d1, d2 = (h22 * g1 - h12 * g2) / det, (h11 * g2 - h12 * g1) / det
        w1, w2 = w1 + d1, w2 + d2
        if abs(d1) + abs(d2) < 1e-6:
            break
    return w1, w2


def model_from(by_band: dict[int, list[tuple[float, float, bool]]], now: float) -> dict[str, Any]:
    bands = []
    for k, (lo, hi) in enumerate(BANDS):
        rows = by_band.get(k, [])
        if len(rows) < MIN_ROWS:
            continue
        w1, w2 = fit(rows)
        bands.append({"lo": lo, "hi": hi, "w1": round(w1, 4), "w2": round(w2, 4), "n": len(rows)})
    return {"built_at": now, "days": FIT_DAYS, "bands": bands}


def rebuild(s: Any, now: float | None = None) -> dict[str, Any]:
    """Fit the weights on the last FIT_DAYS of the tape, for every series a lane with entry_cushion_edge_min trades, and write data/cushion_model.json. The yes side of each moment (every 15 s, spread 0.03 or under), as the study read it."""
    from . import db
    from .lanes import lane_specs
    from .watcher import perp_index

    now = time.time() if now is None else now
    since = now - FIT_DAYS * 86400.0
    series = sorted({sr for sp in lane_specs(s).values() if sp.enabled and sp.entry_cushion_edge_min > -90 for sr in sp.series})
    main = db.connect(s.db_path, "main", shared=True)
    tape = db.connect(s.tape_path, "tape", shared=True)
    by_band: dict[int, list[tuple[float, float, bool]]] = {}
    for ser in series:
        kp = perp_index(ser)
        if not kp:
            continue
        pt: list[float] = []
        pv: list[float] = []
        last = -1
        for r in tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (kp, since - 4 * 3600.0)).fetchall():
            b = int(float(r[0]) // 15)  # 15 s buckets stamped at their END and holding their last tick, so a moment on the 15 s grid reads the last tick at or before it. The first version (30 s buckets stamped at their start) let the fit see up to 30 s past each moment
            if b != last:
                pt.append((b + 1) * 15.0)
                pv.append(float(r[1]))
                last = b
            else:
                pv[-1] = float(r[1])
        for w in main.execute("SELECT ticker, close_ts, result FROM windows WHERE series=? AND close_ts>? AND result IN ('yes','no') ORDER BY close_ts", (ser, since)).fetchall():
            close = float(w["close_ts"])
            opened = close - 900.0
            i0 = bisect.bisect_right(pt, opened + 30.0) - 1
            if i0 < 0 or opened + 30.0 - pt[i0] > 120.0:
                continue
            ref = pv[i0]
            q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], opened, close)).fetchall()
            q = [(float(x[0]), float(x[1] or 0.0), float(x[2] or 0.0)) for x in q if x[2] and float(x[2]) > 0 and not (float(x[1] or 0.0) <= 0 and float(x[2]) >= 1)]
            if len(q) < 50:
                continue
            qt = [x[0] for x in q]
            for t in range(int(opened) + 60, int(close) - 59, 15):
                i = bisect.bisect_right(qt, t) - 1
                if i < 0 or q[i][2] - q[i][1] > 0.03:
                    continue
                left = close - t
                k = next((n for n, (lo, hi) in enumerate(BANDS) if lo <= left < hi), None)
                if k is None:
                    continue
                c = cushion(pt, pv, float(t), ref, "yes", left)
                if c is None:
                    continue
                by_band.setdefault(k, []).append(((q[i][1] + q[i][2]) / 2.0, c, w["result"] == "yes"))
    model = model_from(by_band, now)
    p = Path(s.data_dir) / "cushion_model.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(model))
    tmp.replace(p)
    log.info("cushion model: %d stretches fitted on %d moments of %d series", len(model["bands"]), sum(b["n"] for b in model["bands"]), len(series))
    return model


if __name__ == "__main__":
    from . import config

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    m = rebuild(config.load())
    for b in m["bands"]:
        print(f"{b['lo']:.0f} to {b['hi']:.0f} s left: w1 {b['w1']:.3f}, w2 {b['w2']:+.3f} ({b['n']} moments)")
