"""The curve model: buy a market side because the spot's curve at nine Fibonacci look-backs says it pays (2026-11-07, user: go live).

scripts/state_record_fibonacci.py (studies/results/2026-11-07-state-record-fibonacci.txt): a ridge regression of the bet-to-win net on the curve at 1, 2, 3, 5, 8, 13, 21, 34 and 55 minutes, fit on the earlier days and scored on the next, netted +1.53 per $10 ticket on its best 5% of moments, +1.06 on the best 10% and +0.71 on the best 20% (every moment -0.34), positive in both halves of three test days.
2026-11-08 (user: "you absolutely need to build time left into the curve prediction ... it needs to figure out a further projection based on time remaining"): live bets bought in the first four minutes won 55% (+$15.48 on 31) and those bought after minute 8 won 14% (-$30.54 on 43), while the model read the same curve the same way at minute 3 and minute 12 (studies/results/2026-11-08-multiscale-by-elapsed.txt). The model now also reads the seconds left: the minutes left, and at four scales the cushion (the spot's distance to the strike on the side bought) over what the seconds left can move at that scale's one-second rate (z), and the same after carrying that scale's drift forward to the close (proj), and the normal chance that the cushion is still above zero at the close given proj (win), which the fit sets against the ask. Walk-forward (studies/results/2026-11-08-curve-time-left-walkforward.txt) it did NOT pay: the 18-reading model netted +0.058 per $1 on its best 5% (first per market side) and the timed one -0.024, and at 10% and 20% the timed model was lower in every row. The ridge is fitted on the first 18 readings only; the time-left readings feed the logit_z_drift model below.
2026-11-08 (user: build it with the live switch off; named logit_z_drift for what goes in, because it is a logistic regression and not a probit): a second model reads the same curve through a win chance that rises steadily with the cushion: a logistic regression on the cushion over what the seconds left can move at the 5, 13 and 34 minute rates, the drift terms of the 13 and 34 minute projections, and logit(ask), fit daily with a ridge of 0.01 per row (studies/results/2026-11-08-curve-win-chance-v2.txt: first per market side +0.044 per $1 at an edge bar of 0.05 against the ridge's +0.020, halves -0.099 / +0.116, overfit and miscalibrated at the top). It buys where chance / ask - 1 - fee / ask is at or over the lane's logit_bar. Every model_buy lane logs a "would buy" for it in DATA_DIR/logit_z_drift_shadow.jsonl and scores it hourly against the settled result; a lane buys on it only with model_kind = "logit_z_drift" (default "ridge", the live switch is the user's).
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
import threading
import time
from datetime import datetime
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
READ_NAMES = [f"{kind}_{m}m" for m in FIB for kind in ("slope", "var")]
NAMES = [*READ_NAMES, "ask"]
TIME_SCALES = (2, 5, 13, 34)
TIME_NAMES = ["left_min", *(f"z_{m}m" for m in TIME_SCALES), *(f"proj_{m}m" for m in TIME_SCALES), *(f"win_{m}m" for m in TIME_SCALES)]
CLIP = 9.0
LOGIT_LAM = 0.01
LOGIT_EVERY = 3
LOGIT_NAMES = ["z_5m", "z_13m", "z_34m", "drift_13m", "drift_34m", "logit_ask"]
RETIRE_N = 30
SCORE_EVERY = 3600.0

_fitting: subprocess.Popen | None = None
_cache: dict[str, Any] = {"mtime": None, "data": None}
_logit_cache: dict[str, Any] = {"mtime": None, "data": None}
_logit_lock = threading.Lock()
_logit_seen: set[str] = set()
_logit_scored_at = 0.0


def features(g: list[float | None], sq: list[float], k: int, strike_log: float, sign: int, why: list[str] | None = None, left: float | None = None) -> list[float] | None:
    """The 18 curve readings at index k of a one-second log-price grid g (sq: running sum of squared one-second log steps), plus the 13 time-left readings when `left` (seconds to the close) is given. None when a look-back is not covered or has no spread; `why` then gets the reason."""
    out: list[float] = []
    cushion = (g[k] - strike_log) * sign if g[k] is not None else 0.0
    timed: dict[int, tuple[float, float]] = {}
    for m in FIB:
        look = 60 * m
        if k - look < 0 or g[k] is None or g[k - look] is None:
            if why is not None:
                why.append(f"no price {m} min ago")
            return None
        rate = math.sqrt((sq[k] - sq[k - look]) / look)
        if rate <= 0:
            if why is not None:
                why.append(f"the spot did not move at all over {m} min")
            return None
        unit = rate * math.sqrt(look)
        seg = [(x - strike_log) * sign for x in g[k - look : k + 1 : m] if x is not None]
        if len(seg) < 8:
            if why is not None:
                why.append(f"too few prices over {m} min")
            return None
        mean = sum(seg) / len(seg)
        sd = math.sqrt(sum((x - mean) ** 2 for x in seg) / len(seg))
        out += [((g[k] - g[k - look]) * sign) / unit, sd / unit]
        if m in TIME_SCALES:
            timed[m] = (rate, (g[k] - g[k - look]) * sign / look)
    if left is not None:
        span = max(left, 1.0)
        spread = {m: r * math.sqrt(span) for m, (r, _) in timed.items()}
        out.append(left / 60.0)
        out += [max(-CLIP, min(CLIP, cushion / spread[m])) for m in TIME_SCALES]
        proj = [max(-CLIP, min(CLIP, (cushion + timed[m][1] * span) / spread[m])) for m in TIME_SCALES]
        out += proj
        out += [0.5 * (1.0 + math.erf(p / math.sqrt(2.0))) for p in proj]
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
    names = [*READ_NAMES, *TIME_NAMES, "ask"] if len(rows[0][0]) == len(READ_NAMES) + len(TIME_NAMES) else NAMES
    return {"built_at": time.time(), "n": n, "names": names, "mu": mu, "sd": sd, "w": w, "cuts": cuts, "mean_net": sum(ys) / n, "train_days": TRAIN_DAYS}


def predict(model: dict[str, Any], readings: list[float], ask: float) -> float:
    x = [*readings, ask]
    return model["w"][0] + sum(model["w"][j + 1] * (x[j] - model["mu"][j]) / model["sd"][j] for j in range(len(x)))


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    k = len(b)
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


def _logit(p: float) -> float:
    p = min(max(p, 0.001), 0.999)
    return math.log(p / (1 - p))


def z_drift_inputs(read: list[float], ask: float) -> list[float]:
    """The six inputs of the win-chance model from the 31 readings (18 curve + 13 time-left) and the ask."""
    base = len(READ_NAMES)
    ix = {n: base + j for j, n in enumerate(TIME_NAMES)}
    return [read[ix["z_5m"]], read[ix["z_13m"]], read[ix["z_34m"]], read[ix["proj_13m"]] - read[ix["z_13m"]], read[ix["proj_34m"]] - read[ix["z_34m"]], _logit(ask)]


def fit_logit_z_drift(rows: list[tuple[list[float], float, float]], iters: int = 7) -> dict[str, Any] | None:
    """rows: (the 31 readings, the ask, the net per dollar staked). A logistic regression of win (net above zero) on the six inputs, monotone in every cushion input, ridge LOGIT_LAM per row, on every LOGIT_EVERY-th row (moments in one window overlap). None with too few rows."""
    if len(rows) < MIN_ROWS or len(rows[0][0]) != len(READ_NAMES) + len(TIME_NAMES):
        return None
    sub = rows[::LOGIT_EVERY]
    xs = [z_drift_inputs(r[0], r[1]) for r in sub]
    ys = [1.0 if r[2] > 0 else 0.0 for r in sub]
    n, k = len(xs), len(xs[0])
    mu = [sum(x[j] for x in xs) / n for j in range(k)]
    sd = [(sum((x[j] - mu[j]) ** 2 for x in xs) / n) ** 0.5 or 1.0 for j in range(k)]
    z = [[1.0] + [(x[j] - mu[j]) / sd[j] for j in range(k)] for x in xs]
    lam = LOGIT_LAM * n
    m = k + 1
    w = [0.0] * m
    for _ in range(iters):
        g = [0.0] * m
        h = [[lam if a == b and a else 0.0 for b in range(m)] for a in range(m)]
        for x, y in zip(z, ys, strict=False):
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, sum(a * b for a, b in zip(w, x, strict=False))))))
            d, wt = y - p, p * (1 - p)
            for a in range(m):
                g[a] += d * x[a]
                xa = x[a] * wt
                for b in range(a, m):
                    h[a][b] += xa * x[b]
        for a in range(m):
            for b in range(a):
                h[a][b] = h[b][a]
            if a:
                g[a] -= lam * w[a]
        step = _solve(h, g)
        w = [w[a] + step[a] for a in range(m)]
    return {"kind": "logit_z_drift", "built_at": time.time(), "n": len(rows), "names": LOGIT_NAMES, "mu": mu, "sd": sd, "w": w, "train_days": TRAIN_DAYS}


def predict_logit_z_drift(model: dict[str, Any], read: list[float], ask: float) -> tuple[float, float]:
    """(the win chance, the expected net per dollar staked after the full taker fee at this ask)."""
    from .strategy import fee_per_contract as fee

    x = z_drift_inputs(read, ask)
    t = model["w"][0] + sum(model["w"][j + 1] * (x[j] - model["mu"][j]) / model["sd"][j] for j in range(len(x)))
    p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, t))))
    return p, p / ask - 1.0 - fee(ask) / ask


def load_logit_z_drift(s: Any) -> dict[str, Any] | None:
    p = Path(s.data_dir) / "curve_logit_z_drift.json"
    try:
        m = p.stat().st_mtime
        if _logit_cache["data"] is None or m != _logit_cache["mtime"]:
            _logit_cache["data"], _logit_cache["mtime"] = json.loads(p.read_text()), m
    except (OSError, ValueError):
        return _logit_cache["data"]
    return _logit_cache["data"]


def logit_z_drift_signal(s: Any, shot: Any, spec: Any, ask: float, left: float, chance: float, edge: float, ridge_ok: bool, now: float | None = None) -> None:
    """Shadow: the first time a market side's logit_z_drift edge is at or over the lane's bar, log what that model would buy and whether the ridge did."""
    now = time.time() if now is None else now
    if edge < spec.logit_bar or edge <= 0:
        return
    path = Path(s.data_dir) / "logit_z_drift_shadow.jsonl"
    key = f"{shot.ticker}|{shot.side}|{shot.lane}"
    with _logit_lock:
        if not _logit_seen:
            try:
                for line in path.read_text().splitlines():
                    r = json.loads(line)
                    if r.get("kind") == "signal" and now - r["ts"] < 86400.0:
                        _logit_seen.add(f"{r['ticker']}|{r['side']}|{r['lane']}")
            except (OSError, ValueError, KeyError):
                pass
        if key in _logit_seen:
            return
        _logit_seen.add(key)
    row = {"kind": "signal", "ts": round(now, 1), "ticker": shot.ticker, "lane": shot.lane, "side": shot.side, "ask": round(ask, 4), "left": round(left, 1), "chance": round(chance, 4), "edge": round(edge, 4), "bar": spec.logit_bar, "ridge_buys": bool(ridge_ok)}
    try:
        with path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        log.debug("logit_z_drift shadow: could not write %s", path, exc_info=True)
    log.info("logit_z_drift shadow: %s %s %s at %.2f, %.0fs left, chance %.2f, edge %+.2f per $1 (bar %.2f), ridge %s: would buy", shot.lane, shot.ticker, shot.side, ask, left, chance, edge, spec.logit_bar, "buys" if ridge_ok else "passes")


def run_logit_z_drift_score(s: Any, store: Any, now: float | None = None) -> list[dict[str, Any]]:
    """Hourly: score the shadow signals whose window has settled (net per $1 after the full taker fee, held to the result) and log the 7-day totals for the logit_z_drift model and for the subset the ridge also bought; warn when it is negative over RETIRE_N signals (CLAUDE.md rule 6)."""
    global _logit_scored_at
    from .strategy import fee_per_contract as fee

    now = time.time() if now is None else now
    if now - _logit_scored_at < SCORE_EVERY:
        return []
    _logit_scored_at = now
    path = Path(s.data_dir) / "logit_z_drift_shadow.jsonl"
    try:
        rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        return []
    done = {(r["ticker"], r["side"], r["lane"]) for r in rows if r.get("kind") == "score"}
    new: list[dict[str, Any]] = []
    for sig in rows:
        if sig.get("kind") != "signal" or (sig["ticker"], sig["side"], sig["lane"]) in done:
            continue
        got = store.db.execute("SELECT result FROM windows WHERE ticker=?", (sig["ticker"],)).fetchone()
        if not got or got["result"] not in ("yes", "no"):
            continue
        ask = sig["ask"]
        net = (((1.0 - ask) / ask) if got["result"] == sig["side"] else -1.0) - fee(ask) / ask
        new.append({"kind": "score", "ts": sig["ts"], "ticker": sig["ticker"], "side": sig["side"], "lane": sig["lane"], "net": round(net, 4), "ridge_buys": sig["ridge_buys"]})
    if new:
        try:
            with path.open("a") as fh:
                for line in new:
                    fh.write(json.dumps(line) + "\n")
        except OSError:
            log.debug("logit_z_drift shadow: could not write the scores", exc_info=True)
    recent = [r for r in rows + new if r.get("kind") == "score" and now - r["ts"] <= 7 * 86400.0]
    both = [r for r in recent if r["ridge_buys"]]
    only = [r for r in recent if not r["ridge_buys"]]
    fmt = lambda xs: (len(xs), round(sum(r["net"] for r in xs) / len(xs), 3) if xs else 0.0)  # noqa: E731
    log.info("logit_z_drift shadow (7 d, signals and mean net per $1): all %s, ridge also bought %s, logit_z_drift only %s", fmt(recent), fmt(both), fmt(only))
    if len(recent) >= RETIRE_N and sum(r["net"] for r in recent) < 0:
        log.warning("logit_z_drift shadow: negative over %d signals in 7 days (%.3f per $1): do not turn it on", len(recent), sum(r["net"] for r in recent) / len(recent))
    return new


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
    if not index:
        return False, f"curve model: no settlement index is known for {shot.series}."
    if not got or not got[0]:
        return False, f"curve model: no tick history in memory for {index}."
    if not strike or strike <= 0:
        return False, f"curve model: no strike known yet for {shot.ticker}."
    if got[0][0] > now - 60 * FIB[-1] - 20.0:
        return False, f"curve model: only {now - got[0][0]:.0f}s of {index} history, the 55-minute look-back needs {60 * FIB[-1]}s."
    if got[0][-1] < now - 10.0:
        return False, f"curve model: the last {index} tick is {now - got[0][-1]:.0f}s old."
    grid = live_grid(got[0], got[1], now)
    why: list[str] = []
    timed = len(model["mu"]) == len(READ_NAMES) + len(TIME_NAMES) + 1
    try:
        left: float | None = datetime.fromisoformat(str(shot.close_time)).timestamp() - now
    except (TypeError, ValueError):
        left = None
        if timed:
            return False, f"curve model: no close time known for {shot.ticker}, the model reads the time left."
    read = features(grid[0], grid[1], grid[2], math.log(strike), 1 if shot.side == "yes" else -1, why, left) if grid else None
    if read is None:
        return False, f"curve model: the curve cannot be read for {shot.ticker} ({why[0] if why else 'unreadable history'})."
    net = predict(model, read if timed else read[: len(READ_NAMES)], ask)
    cut = model["cuts"].get(f"{spec.model_top:g}")
    if cut is None:
        cut = min(model["cuts"].items(), key=lambda kv: abs(float(kv[0]) - spec.model_top))[1]
    tag = f"curve model {net:+.2f} per $1 (top {spec.model_top:.0%} from {max(cut, 0.0):+.2f})"
    ridge_ok = net > 0 and net >= cut
    pm = load_logit_z_drift(s)
    if pm is not None and left is not None and len(read) == len(READ_NAMES) + len(TIME_NAMES):
        chance, zedge = predict_logit_z_drift(pm, read, ask)
        logit_z_drift_signal(s, shot, spec, ask, left, chance, zedge, ridge_ok)
        if spec.model_kind == "logit_z_drift":
            ptag = f"curve logit_z_drift {zedge:+.2f} per $1 (chance {chance:.2f}, bar {spec.logit_bar:+.2f})"
            if zedge > 0 and zedge >= spec.logit_bar:
                return True, ptag
            return False, f"curve logit_z_drift: {ptag.replace('curve logit_z_drift ', '')} is under the bar."
    elif spec.model_kind == "logit_z_drift":
        return False, "curve logit_z_drift: no logit_z_drift model fitted yet, or the time left is unknown."
    if ridge_ok:
        return True, tag
    return False, f"curve model: {tag.replace('curve model ', '')} is under the bar."


def build_rows(s: Any, now: float | None = None, meta: list[tuple[float, float, str, str, float]] | None = None, timed: bool = True) -> list[tuple[list[float], float, float]]:
    """(`meta`, when given, gets (close, seconds into the window, ticker, side, the side's bid) per row, for studies; `timed` adds the 13 time-left readings the logit_z_drift model needs; the ridge is fitted on the first 18.) The training rows from the tape: every settled window of the model_buy lanes' series in the trailing TRAIN_DAYS, both sides, every MOMENT_STEP seconds inside the lane's entry window, ask and spread inside the lane's bands."""
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
                read = features(g, sq, k, strike_log, 1 if side == "yes" else -1, None, close - at if timed else None)
                if read is None:
                    continue
                if meta is not None:
                    meta.append((close, float(e), w["ticker"], side, bid))
                won = w["result"] == side
                contracts = 1.0 / ask
                net = ((1 - ask) * contracts if won else -1.0) - fee(ask) * contracts
                rows.append((read, ask, net))
    return rows


def main() -> None:
    from . import config

    s = config.reload()
    rows = build_rows(s)
    model = fit([(r[0][: len(READ_NAMES)], r[1], r[2]) for r in rows])
    if model is None:
        print("not fitted", len(rows), "rows")
        return
    logit_model = fit_logit_z_drift(rows)
    if logit_model is not None:
        lp = Path(s.data_dir) / "curve_logit_z_drift.json"
        ptmp = lp.with_suffix(".tmp")
        ptmp.write_text(json.dumps(logit_model))
        ptmp.replace(lp)
        print("logit_z_drift fitted", logit_model["n"], "rows; weights", [round(x, 3) for x in logit_model["w"]])
    p = Path(s.data_dir) / "curve_model.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(model))
    tmp.replace(p)
    print("fitted", model["n"], "rows; mean net per $1", round(model["mean_net"], 4), "cuts", {k: round(v, 3) for k, v in model["cuts"].items()})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
