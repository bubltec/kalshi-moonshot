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
import time
from pathlib import Path
from typing import Any

from .strategy import fee_per_contract as fee

log = logging.getLogger("moonshot")

NAMES = ["logit_ask", "ask_off", "czb", "tr13", "tr35", "left", "czlb", "vr", "amp15", "pos15", "pos30", "pos60", "mv15", "mv30", "mv60", "sl15", "sl30", "sl60", "cv15", "cv30", "cv60", "ft15", "ft30", "ft60", "dr24", "dr72"]
EXITS = ("hold", "1m", "2m", "3m", "5m")
HORIZONS = (60.0, 120.0, 180.0, 300.0)  # seconds after the buy at which the early-sale exits sell at the bid
TRAIN_HOURS = 24.0
STEP = 60.0  # seconds between training samples of a market
REFIT_EVERY = 3600.0
MIN_ROWS = 4000
RIDGE = 1.0
DEAD = (0.45, 0.55)  # never a bet (user, 2026-11-20)

_fitting: subprocess.Popen[bytes] | None = None


STEPS = ("win_chance_shadow",)  # the buy steps that read this model: a lane naming either needs the refit and the recent outcomes


def wanted(specs: Any) -> bool:
    """Whether any enabled lane names a step that reads the model (the watcher starts the refit and the recent-outcomes refresh only then)."""
    return any(sp.enabled and any(x in sp.buy_chain for x in STEPS) for sp in specs.values())


_refs: dict[str, tuple[float, float, float]] = {}  # index -> (read at, the value 24 h ago, the value 72 h ago): a template a background job keeps in memory (refresh_refs), so a decision reads no database
_need_refs: set[str] = set()
REFS_EVERY = 600.0
_refs_at = 0.0


def refs_for(index: str) -> tuple[float, float] | None:
    r = _refs.get(index)
    return (r[1], r[2]) if r and r[1] > 0 and r[2] > 0 else None


def refresh_refs(s: Any, now: float | None = None) -> bool:
    """Background job: for each index a decision has asked about, the spot 24 h and 72 h ago from the tape (never called from a decision)."""
    global _refs_at
    now = time.time() if now is None else now
    if now - _refs_at < REFS_EVERY or not _need_refs:
        return False
    _refs_at = now
    try:
        from . import db

        tape = db.connect(s.tape_path, "tape")
        for ix in list(_need_refs):
            got = []
            for h in (24.0, 72.0):
                r = tape.execute("SELECT value FROM index_ticks WHERE index_id=? AND ts<=? AND ts>? ORDER BY ts DESC LIMIT 1", (ix, now - h * 3600.0, now - h * 3600.0 - 1800.0)).fetchone()
                got.append(float(r[0]) if r else 0.0)
            _refs[ix] = (now, got[0], got[1])
        return True
    except Exception:
        log.debug("binary risk: the 24 and 72 hour references could not be read", exc_info=True)
        return False


SHAPE_BINS = 12  # the wave shape reads a window as this many equal bin means (a blip is one bin's worth, not the whole shape)


def shape(T: list[float], V: list[float], now: float, mins: int, u15: float) -> tuple[float, float, float]:
    """The wave shape of the last `mins` minutes (user, 2026-11-20: the shape over a period of time: linear, sin, cos, log, ...): the window is cut into SHAPE_BINS equal bins, each bin's mean spot is taken, y = ln(bin mean / the last bin's mean) / u15, and a straight line and a parabola are least-squares fitted on x in [-1, 1] (x = -1 the start of the window, +1 now). Returns (slope b: the straight line's rise from the window's middle to now per half-window, in typical moves; curvature c: the parabola's second-order term, in typical moves, positive = bending up, negative = flattening or a hump; line fit: R-squared of the straight line, 1 = a pure line, low = a wave). A flat window reads (0, 0, 1)."""
    lo = now - 60.0 * mins
    edges = [bisect.bisect_left(T, lo + (60.0 * mins) * k / SHAPE_BINS) for k in range(SHAPE_BINS)] + [bisect.bisect_right(T, now)]
    ys = []
    for k in range(SHAPE_BINS):
        seg = V[edges[k] : max(edges[k + 1], edges[k] + 1)]
        ys.append(sum(seg) / len(seg))
    last = ys[-1]
    y = [math.log(v / last) / u15 for v in ys]
    n = SHAPE_BINS
    x = [-1.0 + 2.0 * (k + 0.5) / n for k in range(n)]
    sx2 = sum(a * a for a in x)
    b = sum(a * c for a, c in zip(x, y)) / sx2  # the line through the centre of x (mean x is 0)
    my = sum(y) / n
    ssy = sum((c - my) ** 2 for c in y)
    # parabola y = a + b x + c x^2 on symmetric x: b is the same, c from the centred x^2
    x2 = [a * a - sx2 / n for a in x]
    sx4 = sum(a * a for a in x2)
    c2 = sum(a * (c - my) for a, c in zip(x2, y)) / sx4 if sx4 > 0 else 0.0
    ss_line = sum((c - my - b * a) ** 2 for a, c in zip(x, y))
    ft = 1.0 - ss_line / ssy if ssy > 1e-18 else 1.0
    return max(-60.0, min(60.0, b)), max(-60.0, min(60.0, c2)), max(0.0, min(1.0, ft))


def readings(T: list[float], V: list[float], now: float, strike: float, left_s: float, refs: tuple[float, float] | None = None, cfg: dict[str, Any] | None = None) -> dict[str, float] | None:
    """The curve readings at `now` from the index's ticks (T times, V values, ascending, the last at or before `now`). None when the history is short or stale. `refs` is the spot 24 h and 72 h ago (a background template live, a lookup in training); without it the drift readings are 0 (unknown). The same code reads the live curve and builds the training rows.

    The spot is read as a vector of its 0-1, 0-3 and 0-5 minute averages (user, 2026-11-20: "you need a vector, blended at 1, 3, 5 mins"; a one-tick blip is not a cushion and Kalshi settles on the last minute's average), and the longer positions and moves (15, 30, 60 minutes) are read from their equal blend. The unit is the typical minute (the 15-minute root-mean-square one-second log move times sqrt(60))."""
    cfg = cfg or {}  # the window study's settings (u_min, uh_min, look, avgs); empty = the model's own windows
    look, avgs = cfg.get("look", (15, 30, 60)), cfg.get("avgs", (1, 3, 5))
    j = bisect.bisect_right(T, now) - 1
    if j < 0 or now - T[j] > 10.0 or strike <= 0 or left_s <= 0:
        return None
    i15, ih = (bisect.bisect_left(T, now - x) for x in (900.0, 3600.0))
    if j - i15 < 300 or j - ih < 1500 or T[i15] > now - 840.0 or T[ih] > now - 3300.0:
        return None
    if V[j] <= 0:
        return None
    def squares(i: int) -> float:
        sq = 0.0
        for k in range(i + 1, j + 1):
            a, b = V[k - 1], V[k]
            if a > 0 and b > 0:
                d = math.log(b / a)
                sq += d * d
        return sq

    iu = bisect.bisect_left(T, now - 60.0 * cfg.get("u_min", 15))
    ihh = bisect.bisect_left(T, now - 60.0 * cfg.get("uh_min", 60))
    sq, sqh = squares(iu), squares(ihh)
    span15, spanh = T[j] - T[iu], T[j] - T[ihh]
    if span15 <= 0 or spanh <= 0 or sq <= 0 or sqh <= 0:
        return None
    u15 = math.sqrt(sq / span15 * 60.0)  # the typical minute over the last 15 minutes
    uh = math.sqrt(sqh / spanh * 60.0)  # and over the last hour
    lm = max(left_s, 1.0) / 60.0

    def cl(x: float, a: float) -> float:
        return max(-a, min(a, x))

    def avg(sec: float) -> float:
        a = V[bisect.bisect_left(T, now - sec) : j + 1]
        return sum(a) / len(a)

    sp = {m: avg(60.0 * m) for m in avgs}
    blend = sum(sp.values()) / len(sp)
    out: dict[str, float] = {"left": lm, "cz": cl(math.log(sp[avgs[1]] / strike) / (u15 * math.sqrt(lm)), 8.0)}
    for m in avgs:
        cz = cl(math.log(sp[m] / strike) / (u15 * math.sqrt(lm)), 8.0)
        out[f"cz{m}"] = cz
        out[f"czl{m}"] = cz * min(lm, 15.0) / 15.0  # the same cushion is worth less as the clock runs down (a straight-line fit cannot see that on its own)
    w15 = V[i15 : j + 1]
    out["vr"] = u15 / uh - 1.0  # volatility ratio: the 15 minute typical minute against the hour's (positive = the market has sped up)
    out["amp15"] = math.log(max(w15) / min(w15)) / u15  # range: the 15 minute high to low, in typical minutes
    out["czb"] = sum(out[f"cz{m}"] for m in avgs) / len(avgs)  # the blended cushion: one reading in the fit (three overlapping cushions got opposite-signed weights, so a negative cushion on a longer average could add to the score)
    out["czlb"] = out["czb"] * min(lm, 15.0) / 15.0
    out["czt"] = cl(math.log(V[j] / strike) / (u15 * math.sqrt(lm)), 8.0)  # the last tick against the strike (the pre-2026-11-20 cushion); read only by the cushion ablation study, not in the model
    out["tr13"] = cl(math.log(sp[avgs[0]] / sp[avgs[1]]) / u15, 30.0)  # the trend: the 1 minute average against the 3 minute one, in typical moves (positive = rising)
    out["tr35"] = cl(math.log(sp[avgs[1]] / sp[avgs[2]]) / u15, 30.0)
    for mins in look:
        i = bisect.bisect_left(T, now - 60.0 * mins)
        w = V[i : j + 1]
        lo, hi = min(w), max(w)
        out[f"pos{mins}"] = (blend - lo) / (hi - lo) if hi > lo else 0.5  # where the blended spot sits in that window's range: 0 its low, 1 its high
        out[f"mv{mins}"] = cl(math.log(blend / V[i]) / u15, 60.0)  # how far it moved over that window, in typical moves
    for mins in look:
        sl, cv, ft = shape(T, V, now, mins, u15)
        out[f"sl{mins}"], out[f"cv{mins}"], out[f"ft{mins}"] = sl, cv, ft
    for hrs, ref in zip((24, 72), refs or (0.0, 0.0), strict=True):
        out[f"dr{hrs}"] = cl(math.log(blend / ref) / (u15 * math.sqrt(hrs * 60.0)), 8.0) if ref and ref > 0 else 0.0  # the drift: the spot's move over the past day / three days, against what a random walk of this market's own typical minute would move
    return out


def side_vector(f: dict[str, float], side: str, ask: float, *unused: float) -> list[float]:
    """The readings turned to one side's view, in NAMES' order: a NO side flips the direction readings and the positions. (`unused`: callers written before the recent YES rate was dropped still pass it.)"""
    ask = min(0.98, max(0.02, ask))
    yes = side == "yes"
    sg = 1.0 if yes else -1.0

    def pos(v: float) -> float:
        return v if yes else 1.0 - v

    return [math.log(ask / (1.0 - ask)), 10.0 * (ask - 0.5) ** 2, sg * f["czb"], sg * f["tr13"], sg * f["tr35"], f["left"], sg * f["czlb"], f["vr"], f["amp15"],
            pos(f["pos15"]), pos(f["pos30"]), pos(f["pos60"]), sg * f["mv15"], sg * f["mv30"], sg * f["mv60"], sg * f["sl15"], sg * f["sl30"], sg * f["sl60"], sg * f["cv15"], sg * f["cv30"], sg * f["cv60"], f["ft15"], f["ft30"], f["ft60"], sg * f["dr24"], sg * f["dr72"]]


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


WIN_NAMES = NAMES[2:]  # the readings without the two ask readings: the win-chance score has no price in it (user, 2026-11-21: "a different calculation aside from the price, which we then use to decide when a bet is a good idea")
WIN_BANDS = (0.5, 0.6, 0.7, 0.8, 0.9, 0.95)  # the score bands whose realised win rate the model carries (a calibration table)


def win_path(s: Any) -> Path:
    return Path(s.data_dir) / "binary_win_model.json"


def win_vector(f: dict[str, float], side: str) -> list[float]:
    """The side's price-free readings, in WIN_NAMES' order (side_vector without its two ask readings)."""
    return side_vector(f, side, 0.5)[2:]


def _sigm(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def fit_win(rows: list[tuple[list[float], list[float], float]], now: float | None = None) -> dict[str, Any]:
    """The win-chance score: a ridge logistic fit of whether the side won at settlement on the price-free readings (the study's winp0: studies/results/2026-11-21-winp0-all-ticks-value-check.txt). rows are build_rows' (side vector, nets, ask); the outcome (the side paid $1) is read back from the held net, and the ask is not a reading in the fit. Carries a calibration table: the realised win rate of the training sides in each predicted band."""
    xs = [r[0][2:] for r in rows]
    ys = [1.0 if r[1][0] * r[2] + r[2] + fee(r[2]) > 0.5 else 0.0 for r in rows]
    n = len(WIN_NAMES)
    mu = [sum(x[i] for x in xs) / len(xs) for i in range(n)]
    sd = [math.sqrt(sum((x[i] - mu[i]) ** 2 for x in xs) / len(xs)) or 1.0 for i in range(n)]
    sub = list(range(0, len(xs), 3))
    vs = [[1.0] + [(xs[j][i] - mu[i]) / sd[i] for i in range(n)] for j in sub]
    K = n + 1
    w = [0.0] * K
    for _ in range(6):
        H = [[0.0] * K for _ in range(K)]
        g = [0.0] * K
        for v, j in zip(vs, sub, strict=True):
            p = _sigm(sum(a * b for a, b in zip(w, v, strict=True)))
            ww = max(p * (1.0 - p), 1e-4)
            for i in range(K):
                g[i] += (ys[j] - p) * v[i]
                for k in range(i, K):
                    H[i][k] += ww * v[i] * v[k]
        for i in range(K):
            H[i][i] += RIDGE if i else 1e-6
            g[i] -= RIDGE * w[i] if i else 0.0
            for k in range(i):
                H[i][k] = H[k][i]
        w = [a + b for a, b in zip(w, _solve(H, g), strict=True)]
    model: dict[str, Any] = {"names": WIN_NAMES, "mu": mu, "sd": sd, "w": w, "built_at": time.time() if now is None else now, "rows": len(rows), "train_hours": TRAIN_HOURS}
    preds = [win_chance(model, x) for x in xs]
    cal = []
    for lo, hi in zip(WIN_BANDS, WIN_BANDS[1:] + (1.01,), strict=True):
        got = [y for p, y in zip(preds, ys, strict=True) if lo <= p < hi]
        cal.append({"lo": lo, "hi": hi, "n": len(got), "win": round(sum(got) / len(got), 4) if got else None})
    model["calibration"] = cal
    return model


def win_chance(model: dict[str, Any], x: list[float]) -> float:
    """The predicted chance the side wins at settlement, from its price-free readings."""
    v = [1.0] + [(a - m) / d for a, m, d in zip(x, model["mu"], model["sd"], strict=True)]
    return _sigm(sum(a * b for a, b in zip(model["w"], v, strict=True)))


def win_read(s: Any, shot: Any, now: float | None = None) -> tuple[float | None, str]:
    """(the side's win chance now, the note) or (None, why not). Memory only; no price, no ask band: every gated tick is read."""
    p = win_path(s)
    try:
        model = json.loads(p.read_text())
    except (OSError, ValueError):
        return None, "win chance: no model fitted yet."
    if model.get("names") != WIN_NAMES:
        return None, "win chance: the model was fitted on older readings, a refit is due."
    f, why = _curve(shot, time.time() if now is None else now)
    if f is None:
        return None, why.replace("binary risk:", "win chance:")
    pw = win_chance(model, win_vector(f, shot.side))
    yes = shot.side == "yes"
    sg = 1.0 if yes else -1.0
    band = next((c for c in model.get("calibration", []) if c["lo"] <= pw < c["hi"] and c["win"] is not None), None)
    cal = f"; its band ({band['lo']:.2f}-{min(band['hi'], 1.0):.2f}) won {band['win']:.2f} of {band['n']} training sides" if band else ""
    note = (f"win chance {pw:.2f}{cal}; cushion {sg * f['czb']:+.1f} (1/3/5 min {sg * f['cz1']:+.1f}/{sg * f['cz3']:+.1f}/{sg * f['cz5']:+.1f}), trend {sg * f['tr13']:+.1f}, "
            f"30-min place {f['pos30'] if yes else 1.0 - f['pos30']:.2f}, 30-min move {sg * f['mv30']:+.1f}, volatility ratio {f['vr']:+.2f}, 24 h drift {sg * f['dr24']:+.1f}, {f['left']:.1f} min left")
    return pw, note



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


MIN_LEFT = 180.0  # no buy inside the last three minutes (user, 2026-11-20: "with the margin thinning towards the end, it should have just avoided the buy"; the top picks bought with under 3 minutes left netted -0.186 per $1, negative on both halves)


def gate(f: dict[str, float], side: str, left_s: float) -> str | None:
    """Why the model does not look at this side at all (None = eligible): the side's cushion must be positive on the 1, 3 and 5 minute averages (a side the spot has not been ahead on is not bought on a trend), and at least three minutes must be left. Curve readings only. Training rows are cut by the same gate, so the fit learns where it acts."""
    sg = 1.0 if side == "yes" else -1.0
    if left_s < MIN_LEFT:
        return f"only {left_s / 60.0:.1f} minutes left (no buy inside the last {MIN_LEFT / 60.0:.0f})"
    weakest = min(sg * f["cz1"], sg * f["cz3"], sg * f["cz5"])
    if weakest <= 0.0:
        return f"the side's weakest cushion is {weakest:+.1f} (it must be ahead on the 1, 3 and 5 minute averages)"
    return None


def _curve(shot: Any, now: float) -> tuple[dict[str, float] | None, str]:
    """The side's curve readings now, gated: (readings, "") or (None, why not). Memory only; no price is read here (the win-chance score uses this alone)."""
    from . import ticks as ticks_mod
    from . import variants

    index = curve_index(shot.series)
    if not index:
        return None, f"binary risk: no Kalshi index or perp reference is read for {shot.series}."
    got = ticks_mod.history.window(index, now - 3700.0, now)
    if not got or not got[0]:
        return None, f"binary risk: no tick history in memory for {index}."
    strike = ticks_mod.strikes.get(shot.ticker) if variants.crypto_index(shot.series) else open_strike(got[0], got[1], now + shot.hours_to_close * 3600.0)
    if not strike or strike <= 0:
        return None, f"binary risk: no strike known yet for {shot.ticker}."
    _need_refs.add(index)
    f = readings(got[0], got[1], now, float(strike), shot.hours_to_close * 3600.0, refs_for(index))
    if f is None:
        return None, f"binary risk: the curve cannot be read yet for {shot.ticker} (short or stale history)."
    why = gate(f, shot.side, shot.hours_to_close * 3600.0)
    if why:
        return None, f"binary risk: {why}."
    return f, ""




def maybe_refit(s: Any, now: float | None = None) -> bool:
    """Background: refresh the recent outcomes (every minute) and start the hourly refit as its own low-priority process. True when a refit was started."""
    global _fitting
    now = time.time() if now is None else now
    refresh_refs(s, now)
    if _fitting is not None and _fitting.poll() is None:
        return False
    wm = None
    try:
        wm = json.loads(win_path(s).read_text())
    except (OSError, ValueError):
        pass
    if wm and wm.get("names") == WIN_NAMES and now - wm["built_at"] < REFIT_EVERY:
        return False
    log.info("binary risk: refitting from the tape (separate process)")
    _fitting = subprocess.Popen([sys.executable, "-m", "kalshi_moonshot.binaryrisk"], preexec_fn=lambda: os.nice(15))  # lowers the child's priority only
    return True


def build_rows(s: Any, now: float) -> list[tuple[list[float], list[float], float]]:
    """The training sides of the last TRAIN_HOURS from the tape: every STEP seconds of every settled 15-minute market (a coin on its settlement index, a commodity on Kalshi's perp reference), both sides, each with its net per $1 under the five exits."""
    from . import db

    st = db.connect(s.db_path, "main", shared=True)
    tape = db.connect(s.tape_path, "tape", shared=True)
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
            ref = []
            for h in (24.0, 72.0):  # the spot 24 h and 72 h before the open: two lookups a market, the drift readings' references
                r = tape.execute("SELECT value FROM index_ticks WHERE index_id=? AND ts<=? AND ts>? ORDER BY ts DESC LIMIT 1", (ix, opened - h * 3600.0, opened - h * 3600.0 - 1800.0)).fetchone()
                ref.append(float(r[0]) if r else 0.0)
            refs = (ref[0], ref[1])
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
                f = readings(T, V, t, strike, close - t, refs) if qq else None
                if f and qq:
                    ahead = [quote_at(t + h) if t + h <= close - 15.0 else None for h in HORIZONS]
                    for side, ask in (("yes", qq[1]), ("no", 1.0 - qq[0])):
                        if not 0.02 <= ask <= 0.98 or gate(f, side, close - t):
                            continue
                        won = win_yes if side == "yes" else not win_yes
                        pays = [1.0 if won else 0.0]
                        for e in ahead:
                            if e is None:
                                pays.append(1.0 if won else 0.0)
                            else:
                                sell = e[0] if side == "yes" else 1.0 - e[1]
                                pays.append(sell - fee(sell))
                        rows.append((side_vector(f, side, ask), [(p - ask - fee(ask)) / ask for p in pays], ask))
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
    win = fit_win(rows, now)
    path = win_path(s)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(win))
    os.replace(tmp, path)
    log.info("binary risk: win chance fitted on %d sides", len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
