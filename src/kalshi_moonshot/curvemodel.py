"""The curve model: buy a market side because the spot's curve at nine Fibonacci look-backs says it pays (2026-11-07, user: go live).

scripts/state_record_fibonacci.py (studies/results/2026-11-07-state-record-fibonacci.txt): a ridge regression of the bet-to-win net on the curve at 1, 2, 3, 5, 8, 13, 21, 34 and 55 minutes, fit on the earlier days and scored on the next, netted +1.53 per $10 ticket on its best 5% of moments, +1.06 on the best 10% and +0.71 on the best 20% (every moment -0.34), positive in both halves of three test days.
Readings, per scale: the SLOPE (the change of the cushion over the look-back in that look-back's typical move; tan of the angle) and the VARIABILITY (the standard deviation of the cushion over the look-back in the same unit). The cushion is the spot's distance to the strike on the side bought, so one curve gives both sides' readings. Plus the price asked. No coin and no crossing count enters.
The model is refit daily from the tape in a separate low-priority process (python -m kalshi_moonshot.curvemodel). A lane with model_buy buys a market side when its predicted net per dollar is in the top `model_top` share of the training moments and above zero.
"""

from __future__ import annotations

import bisect
import itertools
import json
import logging
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("moonshot")

FIB = (1, 2, 3, 5, 8, 13, 21, 34, 55)
REFIT_EVERY = 86400.0
TRAIN_DAYS = 9.0
RIDGE = 50.0
MIN_ROWS = 3000
MOMENT_STEP = 30
QUANTS = (0.05, 0.10, 0.20, 0.30, 0.50)
NAMES = [f"{kind}_{m}m" for m in FIB for kind in ("slope", "var")] + ["ask"]

_fitting: subprocess.Popen | None = None
_cache: dict[str, Any] = {"mtime": None, "data": None}


def features(g: list[float | None], sq: list[float], k: int, strike_log: float, sign: int) -> list[float] | None:
    """The 18 curve readings at index k of a one-second log-price grid g (sq: running sum of squared one-second log steps). None when a look-back is not covered or has no spread."""
    out: list[float] = []
    for m in FIB:
        look = 60 * m
        if k - look < 0 or g[k] is None or g[k - look] is None:
            return None
        rate = math.sqrt((sq[k] - sq[k - look]) / look)
        if rate <= 0:
            return None
        unit = rate * math.sqrt(look)
        seg = [(x - strike_log) * sign for x in g[k - look : k + 1 : m] if x is not None]
        if len(seg) < 8:
            return None
        mean = sum(seg) / len(seg)
        sd = math.sqrt(sum((x - mean) ** 2 for x in seg) / len(seg))
        out += [((g[k] - g[k - look]) * sign) / unit, sd / unit]
    return out


def live_grid(ts: list[float], vs: list[float], now: float) -> tuple[list[float | None], list[float], int] | None:
    """The one-second grid of the last 55 minutes from in-memory ticks: (g, sq, k) with k the index of `now`. None when the history does not cover it."""
    span = 60 * FIB[-1]
    if not ts or ts[0] > now - span - 20.0 or ts[-1] < now - 10.0:
        return None
    t_end = int(now)
    g: list[float | None] = []
    for t in range(t_end - span, t_end + 1):
        i = bisect.bisect_right(ts, t) - 1
        g.append(math.log(vs[i]) if i >= 0 and vs[i] > 0 else None)
    sq = [0.0]
    for a, b in itertools.pairwise(g):
        sq.append(sq[-1] + ((b - a) ** 2 if a is not None and b is not None else 0.0))
    return g, sq, len(g) - 1


def _ridge(xs: list[list[float]], ys: list[float], lam: float = RIDGE) -> list[float]:
    k = len(xs[0])
    a = [[0.0] * k for _ in range(k)]
    b = [0.0] * k
    for x, y in zip(xs, ys, strict=False):
        for i in range(k):
            b[i] += x[i] * y
            xi = x[i]
            ai = a[i]
            for j in range(i, k):
                ai[j] += xi * x[j]
    for i in range(k):
        for j in range(i):
            a[i][j] = a[j][i]
        if i:
            a[i][i] += lam
    m = [[*a[i][:], b[i]] for i in range(k)]
    for c in range(k):
        p = max(range(c, k), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        pv = m[c][c] or 1e-9
        m[c] = [v / pv for v in m[c]]
        for r in range(k):
            if r != c and m[r][c]:
                fac = m[r][c]
                m[r] = [v - fac * w for v, w in zip(m[r], m[c], strict=False)]
    return [m[i][k] for i in range(k)]


def fit(rows: list[tuple[list[float], float, float]]) -> dict[str, Any] | None:
    """rows: (the 18 curve readings, the ask, the net per dollar staked). Returns the model, or None with too few rows."""
    if len(rows) < MIN_ROWS:
        return None
    xs = [[*r[0], r[1]] for r in rows]
    ys = [r[2] for r in rows]
    n, k = len(xs), len(xs[0])
    mu = [sum(x[j] for x in xs) / n for j in range(k)]
    sd = [(sum((x[j] - mu[j]) ** 2 for x in xs) / n) ** 0.5 or 1.0 for j in range(k)]
    z = [[1.0] + [(x[j] - mu[j]) / sd[j] for j in range(k)] for x in xs]
    w = _ridge(z, ys)
    pred = sorted(sum(a * b for a, b in zip(w, row, strict=False)) for row in z)
    cuts = {f"{q:g}": pred[int((1.0 - q) * (n - 1))] for q in QUANTS}
    return {"built_at": time.time(), "n": n, "names": NAMES, "mu": mu, "sd": sd, "w": w, "cuts": cuts, "mean_net": sum(ys) / n, "train_days": TRAIN_DAYS}


def predict(model: dict[str, Any], readings: list[float], ask: float) -> float:
    x = [*readings, ask]
    return model["w"][0] + sum(model["w"][j + 1] * (x[j] - model["mu"][j]) / model["sd"][j] for j in range(len(x)))


def load(s: Any) -> dict[str, Any] | None:
    p = Path(s.data_dir) / "curve_model.json"
    try:
        m = p.stat().st_mtime
        if _cache["data"] is None or m != _cache["mtime"]:
            _cache["data"], _cache["mtime"] = json.loads(p.read_text()), m
    except (OSError, ValueError):
        return _cache["data"]
    return _cache["data"]


def maybe_refit(s: Any, now: float | None = None) -> bool:
    """Start the daily refit as its own low-priority process. True when one was started."""
    global _fitting
    now = time.time() if now is None else now
    if _fitting is not None and _fitting.poll() is None:
        return False
    model = load(s)
    if model and now - model["built_at"] < REFIT_EVERY:
        return False
    log.info("curve model: refitting from the tape (separate process)")
    _fitting = subprocess.Popen([sys.executable, "-m", "kalshi_moonshot.curvemodel"], preexec_fn=lambda: os.nice(15))  # lowers the child's priority only
    return True


def admits(s: Any, store: Any, shot: Any, spec: Any, ask: float, waited: float = 0.0) -> tuple[bool, str]:
    """Whether a model_buy lane would buy this market side at `ask` now: (True, the note tag) or (False, why not)."""
    from . import ticks as ticks_mod
    from . import variants

    model = load(s)
    if model is None:
        return False, "curve model: no model fitted yet."
    index = variants.crypto_index(shot.series)
    strike = ticks_mod.strikes.get(shot.ticker) or store.window_strike(shot.ticker)
    now = time.time()
    got = ticks_mod.history.window(index, now - 60 * FIB[-1] - 40.0, now) if index else None
    grid = live_grid(got[0], got[1], now) if got else None
    if grid is None or not strike or strike <= 0:
        return False, f"curve model: the curve is unreadable for {shot.ticker}."
    g, sq, k = grid
    read = features(g, sq, k, math.log(strike), 1 if shot.side == "yes" else -1)
    if read is None:
        return False, f"curve model: the curve is unreadable for {shot.ticker}."
    net = predict(model, read, ask)
    cut = model["cuts"].get(f"{spec.model_top:g}")
    if cut is None:
        cut = min(model["cuts"].items(), key=lambda kv: abs(float(kv[0]) - spec.model_top))[1]
    tag = f"curve model {net:+.2f} per $1 (top {spec.model_top:.0%} from {max(cut, 0.0):+.2f})"
    if net > 0 and net >= cut:
        return True, tag
    return False, f"curve model: {tag.replace('curve model ', '')} is under the bar."


def build_rows(s: Any, now: float | None = None) -> list[tuple[list[float], float, float]]:
    """The training rows from the tape: every settled window of the model_buy lanes' series in the trailing TRAIN_DAYS, both sides, every MOMENT_STEP seconds inside the lane's entry window, ask and spread inside the lane's bands."""
    from . import db, variants
    from .kalshiws import RECORD_ONLY
    from .lanes import lane_specs
    from .store import Store
    from .strategy import fee_per_contract as fee

    now = time.time() if now is None else now
    rules: dict[str, tuple[float, float, float, float, float]] = {}
    for sp in lane_specs(s).values():
        if sp.enabled and sp.model_buy:
            for series in sp.series:
                lo = max(sp.entry_elapsed[0] * 60.0, 900.0 - sp.hours[1] * 3600.0)
                hi = min(sp.entry_elapsed[1] * 60.0, 900.0 - sp.hours[0] * 3600.0)
                rules[series] = (lo, hi, sp.price[0], sp.price[1], sp.max_spread if sp.max_spread > 0 else 1.0)
    if not rules:
        return []
    store = Store(s.db_path, s.exclude_versions)
    tape = db.connect(s.tape_path, "tape", shared=True)
    t0 = now - TRAIN_DAYS * 86400.0
    g0 = int(t0 - 3500)
    g1 = int(now) + 2
    wins = store.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY series,close_ts", (t0,)).fetchall()
    rows: list[tuple[list[float], float, float]] = []
    cur_ix = None
    g: list[float | None] = []
    sq: list[float] = []
    for w in wins:
        ser = w["series"]
        if ser not in rules or ser in RECORD_ONLY:
            continue
        ix = variants.crypto_index(ser)
        if not ix:
            continue
        if ix != cur_ix:
            cur_ix = ix
            r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, g0 - 60)).fetchall()
            ts = [float(x[0]) for x in r]
            vs = [float(x[1]) for x in r]
            g, j, last = [], 0, None
            for t in range(g0, g1):
                while j < len(ts) and ts[j] <= t:
                    last = vs[j]
                    j += 1
                g.append(math.log(last) if last else None)
            sq = [0.0]
            for a, b in itertools.pairwise(g):
                sq.append(sq[-1] + ((b - a) ** 2 if a is not None and b is not None else 0.0))
        lo, hi, plo, phi, cap = rules[ser]
        close = float(w["close_ts"])
        opened = close - 900.0
        strike_log = math.log(float(w["strike"]))
        q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 20, close)).fetchall()
        if len(q) < 20:
            continue
        qt = [float(x[0]) for x in q]
        for e in range(max(60, int(lo) // MOMENT_STEP * MOMENT_STEP), min(841, int(hi) + 1), MOMENT_STEP):
            if e < lo:
                continue
            at = opened + e
            i = bisect.bisect_right(qt, at) - 1
            if i < 0 or at - qt[i] > 20:
                continue
            yb, ya = float(q[i][1]), float(q[i][2])
            k = int(at) - g0
            if k < 0 or k >= len(g):
                continue
            for side, bid, ask in (("yes", yb, ya), ("no", 1 - ya, 1 - yb)):
                if not (plo <= ask <= phi) or ask - bid > cap:
                    continue
                read = features(g, sq, k, strike_log, 1 if side == "yes" else -1)
                if read is None:
                    continue
                won = w["result"] == side
                contracts = 1.0 / ask
                net = ((1 - ask) * contracts if won else -1.0) - fee(ask) * contracts
                rows.append((read, ask, net))
    return rows


def main() -> None:
    from . import config

    s = config.reload()
    rows = build_rows(s)
    model = fit(rows)
    if model is None:
        print("not fitted", len(rows), "rows")
        return
    p = Path(s.data_dir) / "curve_model.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(model))
    tmp.replace(p)
    print("fitted", model["n"], "rows; mean net per $1", round(model["mean_net"], 4), "cuts", {k: round(v, 3) for k, v in model["cuts"].items()})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
