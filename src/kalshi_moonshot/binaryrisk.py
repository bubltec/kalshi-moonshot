"""The binary risk model as a buy-or-not score (2026-11-20, user: "yes let's try it", after the feature-variant study).

What it says: for one side (YES or NO) of a 15-minute market at this moment, how much profit buying it now is expected to make, net of the fee, under the best of five exits (held to settlement, or sold at the bid 1, 2, 3 or 5 minutes later). It is not a chance of YES and is never compared with Kalshi's price: the price is one of its inputs (the value). A side is a "buy" when its predicted net is in the top 5% of the last day's sides. An ask of 0.45-0.55 is never a bet and an ask under 0.20 is not bet either (the user's rule and the first study's result). Sources: scripts/binary_risk_model.py (the buy-or-not score, studies/results/2026-11-20-binary-risk-model-buy-score.txt), scripts/binary_risk_model_variants.py (twelve feature sets; V5 is the one used here, studies/results/2026-11-20-binary-risk-model-variants.txt: lift over a random point +6.2 and +6.1 points at the top 5% on the two halves of 208 test hours, net +2.1% and +1.7% held, about two standard errors each: not yet shown to pay).

The readings (V5, 16, all from the settlement index's own curve in typical minutes, the 15-minute root-mean-square one-second log move times sqrt(60), time-scaled; turned to the side's view): the price paid (logit ask and its squared distance from 0.5), the cushion in the typical moves left (cz) and in typical minutes (z_tm), minutes left, the 5-minute path efficiency, the 15-minute typical minute against the hour's (vr), the 15-minute range and the spot's place in it, the bounce room on the side's way and against it over 5 minutes, the hour's move and the spot's and the strike's places in the hour's range, and the share of recently settled windows (all series) that resolved YES over the last 1 and 3 hours (the day's drift; past windows only).

How it runs (CLAUDE.md architecture rules): the step reads only memory: the settlement index's tick history (ticks.history), the strike (ticks.strikes), the model file and the recent-outcomes table, which a background job refreshes every minute. The fit is its own low-priority process (`python -m kalshi_moonshot.binaryrisk`), hourly, on the trailing 24 hours of the tape: one ridge regression per exit on standardised readings (ridge 1.0, a constant, the target cut at +3 per $1), the same code that reads the live curve reads the training curve. The step is SHADOW: it never says no, it puts "would buy" or "would not buy" and the score in the decision's note, to be scored against the bets that followed (CLAUDE.md rule 5). Stdlib only (the image has no numpy)."""

from __future__ import annotations

import bisect
import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .strategy import fee_per_contract as fee

log = logging.getLogger("moonshot")

NAMES = ["logit_ask", "ask_off", "cz1", "cz3", "cz5", "zt1", "zt3", "zt5", "left", "eff5", "vr", "amp15", "pos15", "bounce_with", "bounce_against", "h_move", "h_pos", "h_strike", "ry1", "ry3"]
EXITS = ("hold", "1m", "2m", "3m", "5m")
HORIZONS = (60.0, 120.0, 180.0, 300.0)  # seconds after the buy at which the early-sale exits sell at the bid
TOPS = (0.05, 0.10)  # the shares of training sides whose predicted net is stored as a bar
TOP = 0.05  # the bar a buy must reach
TRAIN_HOURS = 24.0
STEP = 60.0  # seconds between training samples of a market
REFIT_EVERY = 3600.0
RECENT_EVERY = 60.0
RECENT_STALE = 900.0  # a recent-outcomes table older than this is not read
MIN_ROWS = 4000
CAP = 3.0  # a net per $1 above this counts as this in the fit, so one long shot cannot lead it
RIDGE = 1.0
ASK_FLOOR = 0.20  # asks under this are not bet (the cheapest band lost in the first study)
DEAD = (0.45, 0.55)  # never a bet (user, 2026-11-20)

_cache: dict[str, Any] = {"mtime": None, "data": None}
_fitting: subprocess.Popen[bytes] | None = None


STEPS = ("binary_risk_shadow", "binary_risk", "swing_risk")  # the buy steps that read this model: a lane naming either needs the refit and the recent outcomes


def wanted(specs: Any) -> bool:
    """Whether any enabled lane names a step that reads the model (the watcher starts the refit and the recent-outcomes refresh only then)."""
    return any(sp.enabled and any(x in sp.buy_chain for x in STEPS) for sp in specs.values())


def model_path(s: Any) -> Path:
    return Path(s.data_dir) / "binary_risk_model.json"


def load(s: Any) -> dict[str, Any] | None:
    p = model_path(s)
    try:
        m = p.stat().st_mtime
        if _cache["data"] is None or m != _cache["mtime"]:
            _cache["data"], _cache["mtime"] = json.loads(p.read_text()), m
    except (OSError, ValueError):
        return _cache["data"]
    return _cache["data"]


def usable(ask: float) -> bool:
    """Whether the model bets at this ask at all: never 0.45-0.55, not under 0.20."""
    return ask >= ASK_FLOOR and not (DEAD[0] <= ask <= DEAD[1])


class Recent:
    """The share of settled windows that resolved YES in the last hours before a moment, from windows closed BEFORE that moment (never the future). Under 20 windows is unknown (0)."""

    def __init__(self, rows: list[tuple[float, int]] | None = None) -> None:
        rows = sorted(rows or [])
        self.closes = [r[0] for r in rows]
        self.cum = [0]
        for _, y in rows:
            self.cum.append(self.cum[-1] + y)
        self.at = time.time()

    def yes(self, t: float, hours: float) -> float:
        lo, hi = bisect.bisect_left(self.closes, t - hours * 3600.0), bisect.bisect_left(self.closes, t)
        return (self.cum[hi] - self.cum[lo]) / (hi - lo) - 0.5 if hi - lo >= 20 else 0.0


_recent = Recent()
_recent.at = 0.0  # nothing read yet: a decision refuses until the first refresh
_recent_at = 0.0


def refresh_recent(s: Any, now: float | None = None) -> bool:
    """Background job: read the last four hours of settled windows into memory (never called from a decision). True when it refreshed."""
    global _recent, _recent_at
    now = time.time() if now is None else now
    if now - _recent_at < RECENT_EVERY:
        return False
    _recent_at = now
    try:
        from . import db

        st = db.connect(s.db_path, "main")
        rows = st.execute("SELECT close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? AND close_ts IS NOT NULL", (now - 4 * 3600.0,)).fetchall()
        _recent = Recent([(float(r["close_ts"]), 1 if r["result"] == "yes" else 0) for r in rows])
        return True
    except Exception:
        log.debug("binary risk: the recent outcomes could not be read", exc_info=True)
        return False


def readings(T: list[float], V: list[float], now: float, strike: float, left_s: float) -> dict[str, float] | None:
    """The curve readings at `now` from the index's ticks (T times, V values, ascending, the last at or before `now`). None when the history is short or stale. The same code reads the live curve and builds the training rows."""
    j = bisect.bisect_right(T, now) - 1
    if j < 0 or now - T[j] > 10.0 or strike <= 0 or left_s <= 0:
        return None
    i15, i5, ih = (bisect.bisect_left(T, now - x) for x in (900.0, 300.0, 3600.0))
    if j - i15 < 300 or j - ih < 1500 or T[i15] > now - 840.0 or T[ih] > now - 3300.0:
        return None
    spot = V[j]
    if spot <= 0:
        return None

    def steps(i: int) -> tuple[float, float]:
        sq = ab = 0.0
        for k in range(i + 1, j + 1):
            a, b = V[k - 1], V[k]
            if a > 0 and b > 0:
                d = math.log(b / a)
                sq += d * d
                ab += abs(d)
        return sq, ab

    sq15, _ = steps(i15)
    sqh, _ = steps(ih)
    _, path5 = steps(i5)
    span15, spanh = T[j] - T[i15], T[j] - T[ih]
    if span15 <= 0 or spanh <= 0 or sq15 <= 0 or sqh <= 0:
        return None
    u15, uh = math.sqrt(sq15 / span15 * 60.0), math.sqrt(sqh / spanh * 60.0)
    w5, w15, wh = V[i5 : j + 1], V[i15 : j + 1], V[ih : j + 1]
    lo15, hi15, loh, hih = min(w15), max(w15), min(wh), max(wh)
    lm = max(left_s, 1.0) / 60.0
    lg = math.log(spot / strike)

    def cl(x: float, a: float) -> float:
        return max(-a, min(a, x))

    def avg(sec: float) -> float:
        a = V[bisect.bisect_left(T, now - sec) : j + 1]
        return sum(a) / len(a)

    # the spot is a vector of its 1, 3 and 5 minute averages, not the last tick (user, 2026-11-20: "you need a vector, blended at 1, 3, 5 mins"; a blip with ten minutes left is not a cushion, and Kalshi settles on the last minute's average)
    lgs = {m: math.log(avg(60.0 * m) / strike) for m in (1, 3, 5)}
    out = {
        "cz": cl(lgs[3] / (u15 * math.sqrt(lm)), 8.0), "left": lm,
        "up5": math.log(max(w5) / spot) / u15, "dn5": math.log(spot / min(w5)) / u15,
        "eff5": abs(math.log(w5[-1] / w5[0])) / path5 if path5 > 0 and w5[0] > 0 and w5[-1] > 0 else 0.0,
        "amp15": math.log(hi15 / lo15) / u15, "pos15": (spot - lo15) / (hi15 - lo15) if hi15 > lo15 else 0.5, "vr": u15 / uh - 1.0,
        "h_move": cl(math.log(spot / wh[0]) / u15, 60.0), "h_pos": (spot - loh) / (hih - loh) if hih > loh else 0.5, "h_strike": (strike - loh) / (hih - loh) if hih > loh else 0.5,
    }
    for m in (1, 3, 5):
        out[f"cz{m}"] = cl(lgs[m] / (u15 * math.sqrt(lm)), 8.0)
        out[f"zt{m}"] = cl(lgs[m] / u15, 30.0)
    return out


def side_vector(f: dict[str, float], side: str, ask: float, ry1: float, ry3: float) -> list[float]:
    """The readings turned to one side's view, in NAMES' order: a NO side flips the direction readings and swaps the bounce and the places."""
    ask = min(0.98, max(0.02, ask))
    yes = side == "yes"
    sg = 1.0 if yes else -1.0
    up, dn = (f["up5"], f["dn5"]) if yes else (f["dn5"], f["up5"])

    def pos(v: float) -> float:
        return v if yes else 1.0 - v

    return [math.log(ask / (1.0 - ask)), 10.0 * (ask - 0.5) ** 2, sg * f["cz1"], sg * f["cz3"], sg * f["cz5"], sg * f["zt1"], sg * f["zt3"], sg * f["zt5"], f["left"], f["eff5"], f["vr"], f["amp15"], pos(f["pos15"]), up, dn,
            sg * f["h_move"], pos(f["h_pos"]), pos(f["h_strike"]), sg * ry1, sg * ry3]


def _solve(A: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    M = [A[i][:] + [b[i]] for i in range(n)]
    for i in range(n):
        p = max(range(i, n), key=lambda k: abs(M[k][i]))
        M[i], M[p] = M[p], M[i]
        d = M[i][i] or 1e-12
        for j in range(i, n + 1):
            M[i][j] /= d
        for k in range(n):
            if k != i:
                f = M[k][i]
                for j in range(i, n + 1):
                    M[k][j] -= f * M[i][j]
    return [M[i][n] for i in range(n)]


def predict(model: dict[str, Any], x: list[float]) -> tuple[float, int]:
    """(the best predicted net per $1 over the five exits, the exit's index)."""
    v = [1.0] + [(a - m) / d for a, m, d in zip(x, model["mu"], model["sd"], strict=True)]
    got = [sum(a * b for a, b in zip(w, v, strict=True)) for w in model["W"]]
    k = max(range(len(got)), key=lambda i: got[i])
    return got[k], k


def ceiling(ask: float) -> float:
    """The most a side bought at `ask` can make per $1 (the fee paid, the side wins): (1 - ask - fee) / ask. A fact of the contract, not a fitted number."""
    ask = min(0.98, max(0.02, ask))
    return (1.0 - ask - fee(ask)) / ask


def predict_capped(model: dict[str, Any], x: list[float], ask: float) -> tuple[float, int]:
    """predict(), cut at what the bet can pay. The fit is a straight line per exit, so at the price extremes it predicts nets that cannot happen (2026-11-20: NO bought at 0.98 on a predicted +0.19 per $1 when it could pay about +0.02)."""
    pred, k = predict(model, x)
    return min(pred, ceiling(ask)), k


def fit(rows: list[tuple[list[float], list[float], float]], now: float | None = None) -> dict[str, Any]:
    """rows: (side vector, net per $1 under each exit, ask). One ridge regression per exit on the standardised readings; the bars are the predicted-net quantiles of the training sides the model bets at."""
    n = len(NAMES)
    mu = [sum(r[0][i] for r in rows) / len(rows) for i in range(n)]
    sd = [math.sqrt(sum((r[0][i] - mu[i]) ** 2 for r in rows) / len(rows)) or 1.0 for i in range(n)]
    K = n + 1
    A = [[0.0] * K for _ in range(K)]
    B = [[0.0] * K for _ in EXITS]
    for r in rows[::3]:
        v = [1.0] + [(r[0][i] - mu[i]) / sd[i] for i in range(n)]
        for i in range(K):
            for j in range(i, K):
                A[i][j] += v[i] * v[j]
            for e in range(len(EXITS)):
                B[e][i] += v[i] * min(CAP, r[1][e])
    for i in range(K):
        A[i][i] += RIDGE if i else 1e-6
        for j in range(i):
            A[i][j] = A[j][i]
    model: dict[str, Any] = {"names": NAMES, "exits": list(EXITS), "mu": mu, "sd": sd, "W": [_solve(A, B[e]) for e in range(len(EXITS))], "built_at": time.time() if now is None else now, "rows": len(rows), "train_hours": TRAIN_HOURS, "capped": True}
    preds = sorted(predict_capped(model, r[0], r[2])[0] for r in rows[::2] if usable(r[2]))
    model["cuts"] = {str(top): preds[min(len(preds) - 1, int(len(preds) * (1.0 - top)))] for top in TOPS} if preds else {}
    return model


NOT_COMMODITY = ("KXINX",)  # the stock index has a Kalshi perp reference but is not one of the commodity series (user, 2026-11-20: "S&P should not be in this"); a scope, not a rule: no buy or sell logic reads it


def curve_index(series: str) -> str | None:
    """The tape index whose curve the model reads for a series: a coin's Kalshi settlement index, else Kalshi's own perp reference for the commodity (perpref.py). Never an outside feed (user, 2026-11-20: "use the kalshi commodity perp"; docs/architecture.md section 10)."""
    from . import perpref
    from .kalshiws import index_for_series

    return index_for_series(series) or perpref.index_for_series(series)


def open_strike(T: list[float], V: list[float], close: float) -> float | None:
    """A commodity market has no settlement-index strike in the tape: its strike is the perp reference's value at the window's open (the first tick within 25 s of it), as the curve model reads it. None when the open was not ticking."""
    opened = close - 900.0
    k = bisect.bisect_left(T, opened)
    if k >= len(T) or T[k] - opened > 25.0 or V[k] <= 0:
        return None
    return V[k]


def _read(s: Any, shot: Any, ask: float, now: float | None = None) -> tuple[dict[str, Any] | None, str]:
    """The model's reading of one side now: ({model, f, pred, k}, "") or (None, why not). Reads memory only."""
    from . import ticks as ticks_mod
    from . import variants

    model = load(s)
    if model is None or not model.get("cuts"):
        return None, "binary risk: no model fitted yet."
    if model.get("names") != NAMES:
        return None, "binary risk: the model was fitted on older readings, a refit is due."
    if not usable(ask):
        return None, f"binary risk: ask {ask:.2f} is not a bet (never 0.45-0.55, not under {ASK_FLOOR:.2f})."
    index = curve_index(shot.series)
    if not index:
        return None, f"binary risk: no Kalshi index or perp reference is read for {shot.series}."
    now = time.time() if now is None else now
    got = ticks_mod.history.window(index, now - 3700.0, now)
    if not got or not got[0]:
        return None, f"binary risk: no tick history in memory for {index}."
    strike = ticks_mod.strikes.get(shot.ticker) if variants.crypto_index(shot.series) else open_strike(got[0], got[1], now + shot.hours_to_close * 3600.0)
    if not strike or strike <= 0:
        return None, f"binary risk: no strike known yet for {shot.ticker}."
    if now - _recent.at > RECENT_STALE:
        return None, "binary risk: the recent outcomes have not been refreshed."
    f = readings(got[0], got[1], now, float(strike), shot.hours_to_close * 3600.0)
    if f is None:
        return None, f"binary risk: the curve cannot be read yet for {shot.ticker} (short or stale history)."
    pred, k = predict_capped(model, side_vector(f, shot.side, ask, _recent.yes(now, 1.0), _recent.yes(now, 3.0)), ask)
    return {"model": model, "f": f, "pred": pred, "k": k}, ""


def admits(s: Any, shot: Any, ask: float, now: float | None = None) -> tuple[bool, str]:
    """Whether the model would buy this market side at `ask` now: (True or False, the note). Reads memory only."""
    got, why = _read(s, shot, ask, now)
    if got is None:
        return False, why
    model, f, pred, k = got["model"], got["f"], got["pred"], got["k"]
    cut = float(model["cuts"].get(str(TOP), 0.0))
    yes = shot.side == "yes"  # the note reads the side's own view: a NO side's cushion is the YES one's sign flipped
    tag = f"binary risk {pred:+.2f}/$1 by {EXITS[k]} (top {TOP:.0%} from {cut:+.2f}; cushion {f['cz'] if yes else -f['cz']:+.1f}, efficiency {f['eff5']:.2f}, hour place {f['h_pos'] if yes else 1.0 - f['h_pos']:.2f})"
    return pred >= cut, tag


SWING_KEEP = 3 * 3600.0  # the market's own recent predictions: the last three hours
SWING_MIN = 200  # fewer own predictions than this and the bar is not known: no buy
SWING_TOP = 0.03  # high scrutiny: the side's prediction must be in the top 3% of that market series' own recent predictions
SWING_GAP = 30.0  # one stored prediction per (market, side) per this many seconds
_swing: dict[str, Any] = {"hist": {}, "last": {}, "lock": threading.Lock()}


def swing_admits(s: Any, shot: Any, ask: float, now: float | None = None) -> tuple[bool, str]:
    """The early-swing score (2026-11-20, user via parent: a paper lane for the early cheap-side swing, "a high-scrutiny risk model, bar from the market's own recent predictions"): the same model, read for a cheap side. The side must (1) have an early SALE (1, 2, 3 or 5 minutes) as the model's best exit, not holding to settlement, and (2) have a predicted net in the top SWING_TOP of the predictions this market series has made at the lane's prices over the last three hours (a bar from its own recent history, CLAUDE.md rule 2; no asset key: the history is kept per market series because that is the market's own curve). Fewer than SWING_MIN own predictions: no buy. Reads memory only."""
    got, why = _read(s, shot, ask, now)
    if got is None:
        return False, why
    now = time.time() if now is None else now
    pred, k = got["pred"], got["k"]
    key = shot.series or shot.ticker.split("-")[0]
    with _swing["lock"]:
        hist = _swing["hist"].setdefault(key, deque())
        while hist and hist[0][0] < now - SWING_KEEP:
            hist.popleft()
        vals = sorted(v for _, v in hist)
        sk = (shot.ticker, shot.side)
        if now - _swing["last"].get(sk, 0.0) >= SWING_GAP:
            _swing["last"][sk] = now
            hist.append((now, pred))
    if len(vals) < SWING_MIN:
        return False, f"swing risk: only {len(vals)} own predictions in the last 3 h for {key}, the bar is not known (needs {SWING_MIN})."
    bar = vals[min(len(vals) - 1, int(len(vals) * (1.0 - SWING_TOP)))]
    tag = f"swing risk {pred:+.2f}/$1 by {EXITS[k]} (bar {bar:+.2f}, the top {SWING_TOP:.0%} of {len(vals)} own predictions in 3 h)"
    if k == 0:
        return False, tag + ": the best exit is to hold, not a swing."
    return pred >= bar, tag


def maybe_refit(s: Any, now: float | None = None) -> bool:
    """Background: refresh the recent outcomes (every minute) and start the hourly refit as its own low-priority process. True when a refit was started."""
    global _fitting
    now = time.time() if now is None else now
    refresh_recent(s, now)
    if _fitting is not None and _fitting.poll() is None:
        return False
    m = load(s)
    if m and m.get("capped") and m.get("names") == NAMES and now - m["built_at"] < REFIT_EVERY:  # a model without the flag has a bar made from uncapped predictions: refit it at once
        return False
    log.info("binary risk: refitting from the tape (separate process)")
    _fitting = subprocess.Popen([sys.executable, "-m", "kalshi_moonshot.binaryrisk"], preexec_fn=lambda: os.nice(15))  # lowers the child's priority only
    return True


def build_rows(s: Any, now: float) -> list[tuple[list[float], list[float], float]]:
    """The training sides of the last TRAIN_HOURS from the tape: every STEP seconds of every settled 15-minute market (a coin on its settlement index, a commodity on Kalshi's perp reference), both sides, each with its net per $1 under the five exits."""
    from . import db

    st = db.connect(s.db_path, "main", shared=True)
    tape = db.connect(s.tape_path, "tape", shared=True)
    allw = st.execute("SELECT close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts IS NOT NULL").fetchall()
    recent = Recent([(float(r["close_ts"]), 1 if r["result"] == "yes" else 0) for r in allw])
    wins = st.execute("SELECT ticker, series, close_ts, strike, result FROM windows WHERE result IN ('yes','no') AND close_ts BETWEEN ? AND ?", (now - TRAIN_HOURS * 3600.0, now)).fetchall()
    by_index: dict[str, list[Any]] = {}
    for w in wins:
        if w["series"].startswith(NOT_COMMODITY):
            continue
        ix = curve_index(w["series"])
        if ix:
            by_index.setdefault(ix, []).append(w)
    from .variants import crypto_index as variants_crypto

    rows: list[tuple[list[float], list[float], float]] = []
    for ix, ws in by_index.items():
        lo, hi = min(float(w["close_ts"]) for w in ws) - 900 - 4000, max(float(w["close_ts"]) for w in ws) + 5
        tk = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? ORDER BY ts", (ix, lo, hi)).fetchall()
        T, V = [float(r[0]) for r in tk], [float(r[1]) for r in tk]
        for w in ws:
            close, opened, win_yes = float(w["close_ts"]), float(w["close_ts"]) - 900.0, w["result"] == "yes"
            strike = float(w["strike"]) if variants_crypto(w["series"]) and w["strike"] and float(w["strike"]) > 0 else open_strike(T, V, close)
            if not strike:
                continue
            q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], opened - 60, close)).fetchall()
            if len(q) < 20:
                continue
            qt, qb, qa = [float(r[0]) for r in q], [float(r[1]) for r in q], [float(r[2]) for r in q]
            scale = 100.0 if max(qa) > 1.5 else 1.0

            def quote_at(t: float) -> tuple[float, float] | None:
                k = bisect.bisect_right(qt, t) - 1
                return (qb[k] / scale, qa[k] / scale) if k >= 0 and t - qt[k] <= 60.0 else None

            t = opened + 60.0
            while t <= close - 15.0:
                qq = quote_at(t)
                f = readings(T, V, t, strike, close - t) if qq else None
                if f and qq:
                    r1, r3 = recent.yes(t, 1.0), recent.yes(t, 3.0)
                    ahead = [quote_at(t + h) if t + h <= close - 15.0 else None for h in HORIZONS]
                    for side, ask in (("yes", qq[1]), ("no", 1.0 - qq[0])):
                        if not 0.02 <= ask <= 0.98:
                            continue
                        won = win_yes if side == "yes" else not win_yes
                        pays = [1.0 if won else 0.0]
                        for e in ahead:
                            if e is None:
                                pays.append(1.0 if won else 0.0)
                            else:
                                sell = e[0] if side == "yes" else 1.0 - e[1]
                                pays.append(sell - fee(sell))
                        rows.append((side_vector(f, side, ask, r1, r3), [(p - ask - fee(ask)) / ask for p in pays], ask))
                t += STEP
    return rows


def main() -> int:
    from . import config

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    s = config.reload()
    now = time.time()
    rows = build_rows(s, now)
    if len(rows) < MIN_ROWS:
        log.warning("binary risk: only %d training sides, the model is not refit", len(rows))
        return 1
    model = fit(rows, now)
    p = model_path(s)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(model))
    os.replace(tmp, p)
    log.info("binary risk: fitted on %d sides, bars %s", len(rows), {k: round(v, 3) for k, v in model["cuts"].items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
