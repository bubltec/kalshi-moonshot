"""The curve model: buy a market side because the spot's curve at nine Fibonacci look-backs says it pays (2026-11-07, user: go live).

scripts/state_record_fibonacci.py (studies/results/2026-11-07-state-record-fibonacci.txt): a ridge regression of the bet-to-win net on the curve at 1, 2, 3, 5, 8, 13, 21, 34 and 55 minutes, fit on the earlier days and scored on the next, netted +1.53 per $10 ticket on its best 5% of moments, +1.06 on the best 10% and +0.71 on the best 20% (every moment -0.34), positive in both halves of three test days.
2026-11-08 (user: "you absolutely need to build time left into the curve prediction ... it needs to figure out a further projection based on time remaining"): live bets bought in the first four minutes won 55% (+$15.48 on 31) and those bought after minute 8 won 14% (-$30.54 on 43), while the model read the same curve the same way at minute 3 and minute 12 (studies/results/2026-11-08-multiscale-by-elapsed.txt). The model now also reads the seconds left: the minutes left, and at four scales the cushion (the spot's distance to the strike on the side bought) over what the seconds left can move at that scale's one-second rate (z), and the same after carrying that scale's drift forward to the close (proj), and the normal chance that the cushion is still above zero at the close given proj (win), which the fit sets against the ask. Walk-forward (studies/results/2026-11-08-curve-time-left-walkforward.txt) it did NOT pay: the 18-reading model netted +0.058 per $1 on its best 5% (first per market side) and the timed one -0.024, and at 10% and 20% the timed model was lower in every row. The ridge is fitted on the first 18 readings only; the time-left readings stay in `features` and `build_rows(timed=True)` for the studies (scripts/curve_time_left_walkforward.py and the win-chance studies). A second model on them (a logistic regression on the cushion and its drift, shadowed 2026-11-08) turned negative over 89 shadow signals (-0.277 per $1) and was retired and removed.
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
PACE_DUTY = 0.3  # 2026-11-09 (user: make sure it paces itself): the daily refit uses at most this share of the wall time. It sleeps after each stretch of work for the rest, so a refit that now trains on the commodity series as well as the crypto ones leaves the other 2-CPU host's bot and its remote-command agent the CPU (a manual refit pinned the host at 98% and cut the agent off on 11-09)
PACE_MAX_PAUSE = 30.0  # ...and never sleeps longer than this at once
QUANTS = (0.05, 0.10, 0.20, 0.30, 0.50)
READ_NAMES = [f"{kind}_{m}m" for m in FIB for kind in ("slope", "var")]
NAMES = [*READ_NAMES, "ask"]
TIME_SCALES = (2, 5, 13, 34)
TIME_NAMES = ["left_min", *(f"z_{m}m" for m in TIME_SCALES), *(f"proj_{m}m" for m in TIME_SCALES), *(f"win_{m}m" for m in TIME_SCALES)]
CLIP = 9.0
QUAD_WINDOWS = (5, 13, 34)  # minutes
QUAD_SAMPLES = 25
QUAD_NAMES = [f"{kind}_{m}m" for m in QUAD_WINDOWS for kind in ("level", "slope", "curv")]
KINDS = {"fibonacci": READ_NAMES, "curvature": QUAD_NAMES}


def _parabola_weights() -> list[list[float]]:
    """(X'X)^-1 X' for the design [1, u, u^2] on QUAD_SAMPLES points u from -1 to 0: three rows of weights, level a, slope b and curvature c of the least-squares parabola."""
    us = [-1.0 + i / (QUAD_SAMPLES - 1) for i in range(QUAD_SAMPLES)]
    xtx = [[sum((1.0, u, u * u)[i] * (1.0, u, u * u)[j] for u in us) for j in range(3)] for i in range(3)]
    a = [[*xtx[i], *(1.0 if i == j else 0.0 for j in range(3))] for i in range(3)]
    for c in range(3):
        p = max(range(c, 3), key=lambda r: abs(a[r][c]))
        a[c], a[p] = a[p], a[c]
        pv = a[c][c]
        a[c] = [x / pv for x in a[c]]
        for r in range(3):
            if r != c:
                f = a[r][c]
                a[r] = [x - f * y for x, y in zip(a[r], a[c], strict=False)]
    inv = [row[3:] for row in a]
    return [[sum(inv[i][j] * (1.0, u, u * u)[j] for j in range(3)) for u in us] for i in range(3)]


_PARABOLA = _parabola_weights()

_fitting: subprocess.Popen | None = None
_cache: dict[str, Any] = {"mtime": None, "data": None}


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


def features_curvature(g: list[float | None], sq: list[float], k: int, strike_log: float, sign: int, why: list[str] | None = None) -> list[float] | None:
    """The curvature readings at index k: over the last 5, 13 and 34 minutes the cushion path (QUAD_SAMPLES points, in that window's typical move: the one-second root-mean-square log move times sqrt(window seconds)) is fitted with a parabola a + b u + c u^2, u from -1 (the window's start) to 0 (now); the readings are its level a (the cushion now, smoothed), slope b and curvature c, per window. One curve gives both sides' readings (all three flip sign with the side). None when a window is not covered or has no spread."""
    out: list[float] = []
    for m in QUAD_WINDOWS:
        look = 60 * m
        if k - look < 0:
            if why is not None:
                why.append(f"no price {m} min ago")
            return None
        rate = math.sqrt((sq[k] - sq[k - look]) / look)
        if rate <= 0:
            if why is not None:
                why.append(f"the spot did not move at all over {m} min")
            return None
        unit = rate * math.sqrt(look)
        ys: list[float] = []
        for i in range(QUAD_SAMPLES):
            x = g[k - look + round(i * look / (QUAD_SAMPLES - 1))]
            if x is None:
                if why is not None:
                    why.append(f"a gap in the prices over {m} min")
                return None
            ys.append((x - strike_log) * sign / unit)
        out += [sum(w[i] * ys[i] for i in range(QUAD_SAMPLES)) for w in _PARABOLA]
    return out


def readings_for(kind: str, g: list[float | None], sq: list[float], k: int, strike_log: float, sign: int, why: list[str] | None = None) -> list[float] | None:
    return features_curvature(g, sq, k, strike_log, sign, why) if kind == "curvature" else features(g, sq, k, strike_log, sign, why)


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


def fit(rows: list[tuple[list[float], float, float]], kind: str = "fibonacci") -> dict[str, Any] | None:
    """rows: (the curve readings of `kind`, the ask, the net per dollar staked). Returns the model, or None with too few rows."""
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
    names = [*KINDS[kind], "ask"] if kind != "fibonacci" else ([*READ_NAMES, *TIME_NAMES, "ask"] if len(rows[0][0]) == len(READ_NAMES) + len(TIME_NAMES) else NAMES)
    return {"kind": kind, "built_at": time.time(), "n": n, "names": names, "mu": mu, "sd": sd, "w": w, "cuts": cuts, "mean_net": sum(ys) / n, "train_days": TRAIN_DAYS}


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


def cut_for(model: dict[str, Any], top: float) -> float:
    """The predicted-net bar for buying the top `top` share of the training moments: the stored quantile when `top` is one of QUANTS, otherwise linear between the two stored quantiles either side of it (0.15 sits between the 0.10 and the 0.20 bars), clamped at the ends."""
    pts = sorted((float(q), float(v)) for q, v in model["cuts"].items())
    for q, v in pts:
        if abs(q - top) < 1e-9:
            return v
    if top <= pts[0][0]:
        return pts[0][1]
    if top >= pts[-1][0]:
        return pts[-1][1]
    for (q0, v0), (q1, v1) in itertools.pairwise(pts):
        if q0 < top < q1:
            return v0 + (v1 - v0) * (top - q0) / (q1 - q0)
    return pts[-1][1]


def admits(s: Any, store: Any, shot: Any, spec: Any, ask: float, waited: float = 0.0) -> tuple[bool, str]:
    """Whether a model_buy lane would buy this market side at `ask` now: (True, the note tag) or (False, why not)."""
    from . import ticks as ticks_mod
    from . import variants

    model = load(s)
    if model is None:
        return False, "curve model: no model fitted yet."
    crypto = variants.crypto_index(shot.series)
    index = crypto or variants.biquote_index(shot.series)  # a commodity reads its BiQuote index against that index's own price when the window opened (the settlement is Pyth's, on another level), as the spot exits and the state record do
    now = time.time()
    if crypto:
        strike = ticks_mod.strikes.get(shot.ticker) or store.window_strike(shot.ticker)
    else:
        strike = ticks_mod.history.first_at_or_after(index, now + shot.hours_to_close * 3600.0 - 905.0, 25.0) if index else None
    got = ticks_mod.history.window(index, now - 60 * FIB[-1] - 40.0, now) if index else None
    if not index:
        return False, f"curve model: no settlement index is known for {shot.series}."
    if not got or not got[0]:
        return False, f"curve model: no tick history in memory for {index}."
    if not strike or strike <= 0:
        return False, f"curve model: no {'strike' if crypto else 'window-open price of ' + index} known yet for {shot.ticker}."
    if got[0][0] > now - 60 * FIB[-1] - 20.0:
        return False, f"curve model: only {now - got[0][0]:.0f}s of {index} history, the 55-minute look-back needs {60 * FIB[-1]}s."
    if got[0][-1] < now - 10.0:
        return False, f"curve model: the last {index} tick is {now - got[0][-1]:.0f}s old."
    grid = live_grid(got[0], got[1], now)
    why: list[str] = []
    kind = model.get("kind", "fibonacci")
    if kind != spec.model_features:
        return False, f"curve model: the fitted model reads {kind} readings and this lane wants {spec.model_features}: waiting for the refit."
    read = readings_for(kind, grid[0], grid[1], grid[2], math.log(strike), 1 if shot.side == "yes" else -1, why) if grid else None
    if read is None:
        return False, f"curve model: the curve cannot be read for {shot.ticker} ({why[0] if why else 'unreadable history'})."
    net = predict(model, read, ask)
    cut = cut_for(model, spec.model_top)
    tag = f"curve model {net:+.2f} per $1 (top {spec.model_top:.0%} from {max(cut, 0.0):+.2f})"
    if net > 0 and net >= cut:
        return True, tag
    return False, f"curve model: {tag.replace('curve model ', '')} is under the bar."


class Pacer:
    """Duty-cycle pacing for a long job: `tick()` sleeps for as long as the work since the last tick took times (1/duty - 1), so the job uses about `duty` of the wall time. duty of 1 (or 0) never sleeps."""

    def __init__(self, duty: float = 1.0) -> None:
        self.duty = duty
        self.mark = time.monotonic()

    def tick(self) -> None:
        if 0.0 < self.duty < 1.0:
            time.sleep(min(PACE_MAX_PAUSE, (time.monotonic() - self.mark) * (1.0 / self.duty - 1.0)))
        self.mark = time.monotonic()


def build_rows(s: Any, now: float | None = None, meta: list[tuple[float, float, str, str, float]] | None = None, timed: bool = False, kind: str = "fibonacci", duty: float = 1.0) -> list[tuple[list[float], float, float]]:
    """(`meta`, when given, gets (close, seconds into the window, ticker, side, the side's bid) per row, for studies; `timed` adds the 13 time-left readings for the studies; the ridge is fitted on the first 18.) The training rows from the tape: every settled window of the model_buy lanes' series in the trailing TRAIN_DAYS, both sides, every MOMENT_STEP seconds inside the lane's entry window, ask and spread inside the lane's bands."""
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
    pace = Pacer(duty)
    for w in wins:
        pace.tick()
        ser = w["series"]
        if ser not in rules or ser in RECORD_ONLY:
            continue
        crypto = variants.crypto_index(ser)
        ix = crypto or variants.biquote_index(ser)
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
            pace.tick()
        lo, hi, plo, phi, cap = rules[ser]
        close = float(w["close_ts"])
        opened = close - 900.0
        if crypto:
            strike_log = math.log(float(w["strike"]))
        else:  # a commodity: the index's own price at the window open (the first tick from 5 s before to 20 s after), as the live lane reads it
            a = bisect.bisect_left(ts, opened - 5.0)
            if a >= len(ts) or ts[a] > opened + 20.0:
                continue
            strike_log = math.log(vs[a])
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
                if kind == "curvature":
                    read = features_curvature(g, sq, k, strike_log, 1 if side == "yes" else -1)
                else:
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
    from .lanes import lane_specs

    kinds = [sp.model_features for sp in lane_specs(s).values() if sp.enabled and sp.model_buy]
    kind = kinds[0] if kinds else "fibonacci"
    if len(set(kinds)) > 1:
        print("model_buy lanes disagree on model_features", sorted(set(kinds)), "-> fitting", kind)
    rows = build_rows(s, kind=kind, duty=PACE_DUTY)
    model = fit([(r[0][: len(KINDS[kind])], r[1], r[2]) for r in rows], kind=kind)
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
