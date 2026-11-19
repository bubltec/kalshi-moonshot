"""The context score: what the readings logged at a buy, TOGETHER, say about its chance to win beyond what its price says (2026-11-19, user: "there's so many different variables in play and you keep isolating individual features like price or time when its a combination"; "yes", to a shadow step).

A buy module step (`context_score` in a lane's buy module steps) that is SHADOW: it never says no. When the lane's bet row is written, the note gets one reading: the model's chance that the bet wins, the shortfall of that chance under the ask, and where the shortfall sits among the training bets (a percentile). scripts/context_score_forward.py scores it forward on the bets that follow: does the shortfall predict the bets that lose beyond their price, and what skipping the top fifth would have done. It gates nothing until the user says so, after at least a week of live bets (CLAUDE.md rule 5); a gate needs the readings in memory at the decision (they are read from the tape when the row is written), which is not built.

The model is a ridge logistic regression for a bet's chance to WIN with the price as an offset (the logit of the ask), so it only learns what the readings add to the price: the readings are those the decision row already carries (the `factors` snapshot and the numbers in the context and reason text), none a market or coin name. Fitted by a daily low-priority process (maybe_refit, like the curve model) on every settled live bet, in plain Python (the image has no numpy); lambda is fixed at 300 (scripts/commodity_bet_combination.py picked it on its training bets; 822 bets, out of sample the model did not beat the price on log loss, so this is a measuring instrument, not a proven edge). Memory only at a decision: the model is a small file read when it changes."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("moonshot")

REFIT_EVERY = 86400.0
LAMBDA = 300.0
NUMERIC = ("ask", "left", "elapsed_min", "spread", "imbalance", "move_90", "range_90", "rise_30", "rise_90", "dip_90", "held_min", "updates_60", "vol", "z", "spike_z", "width_pct", "model_p_side", "rec_weight", "rec_skill")
CATEGORICAL = ("mtype", "variety")
_cache: dict[str, Any] = {"mtime": None, "data": None}
_fitting: subprocess.Popen | None = None


def model_path(s: Any) -> Path:
    return Path(s.data_dir) / "context_score.json"


def _tags(text: str, side: str) -> dict[str, Any]:
    """The numbers the decision text carries (context and reason): the same ones scripts/export_commodity_bets.py reads."""
    out: dict[str, Any] = {}
    m = re.search(r"P\(yes\) ([0-9.]+)", text)
    if m:
        py = float(m.group(1))
        out["model_p_side"] = py if side == "yes" else 1.0 - py
    for key, pat in (("width_pct", r"width ([0-9.]+)%"), ("vol", r"\[vol ([0-9.]+)x\]"), ("z", r"\[z ([+-]?[0-9.]+)\]"), ("spike_z", r"\[spike view z ([+-]?[0-9.]+)")):
        m = re.search(pat, text)
        if m:
            out[key] = float(m.group(1))
    m = re.search(r"weight on estimate ([0-9.]+), skill (-?[0-9.]+)", text)
    if m:
        out["rec_weight"], out["rec_skill"] = float(m.group(1)), float(m.group(2))
    m = re.search(r"\[mtype ([a-z+_-]+)\]", text)
    if m:
        out["mtype"] = m.group(1)
    m = re.search(r"\[variety ([A-Z][-a-z]*)\]", text)
    if m:
        out["variety"] = m.group(1)
    return out


def readings(factors: dict[str, Any], text: str, ask: float, side: str, left: float) -> dict[str, Any]:
    """Every reading the model uses, from one decision: the factors snapshot, the decision text, the ask, the side and the seconds to the close."""
    r: dict[str, Any] = {k: factors.get(k) for k in ("elapsed_min", "spread", "imbalance", "move_90", "range_90", "rise_30", "rise_90", "dip_90", "held_min", "updates_60")}
    r.update(_tags(text, side))
    r["ask"], r["left"] = ask, min(max(left, 0.0), 1000.0)
    act = factors.get("activity_60")
    r["log_activity_60"] = math.log1p(float(act)) if act is not None else None
    imb = factors.get("imbalance")
    r["imbalance_x_side"] = float(imb) * (1.0 if side == "yes" else -1.0) if imb is not None else None
    return r


def _vector(model: dict[str, Any], r: dict[str, Any]) -> list[float]:
    """The model's feature vector for one set of readings: the median where a reading is missing, with the missing flags and one-hot categories the training bets had."""
    out = []
    for name in model["features"]:
        if name.endswith("_missing"):
            out.append(1.0 if r.get(name[: -len("_missing")]) is None else 0.0)
        elif "=" in name:
            col, val = name.split("=", 1)
            out.append(1.0 if r.get(col) == val else 0.0)
        else:
            v = r.get(name)
            out.append(float(v) if v is not None else float(model["med"].get(name, 0.0)))
    return out


def predict(model: dict[str, Any], r: dict[str, Any]) -> float:
    """The model's chance that the bet wins."""
    x = _vector(model, r)
    z = model["b"][0] + sum(b * min(5.0, max(-5.0, (v - m) / sd)) for b, v, m, sd in zip(model["b"][1:], x, model["mu"], model["sd"], strict=True))  # a reading far outside the training range counts as five standard deviations at most
    ask = min(max(float(r["ask"]), 1e-4), 1 - 1e-4)
    return 1.0 / (1.0 + math.exp(-min(30.0, max(-30.0, z + math.log(ask / (1.0 - ask))))))


def load(s: Any) -> dict[str, Any] | None:
    p = model_path(s)
    try:
        m = p.stat().st_mtime
        if _cache["data"] is None or m != _cache["mtime"]:
            _cache["data"], _cache["mtime"] = json.loads(p.read_text()), m
    except (OSError, ValueError):
        return _cache["data"]
    return _cache["data"]


def percentile(model: dict[str, Any], shortfall: float) -> float:
    """Where a shortfall sits among the training bets' (0 to 100)."""
    qs = model.get("quantiles") or []
    if not qs:
        return 50.0
    return 100.0 * sum(1 for q in qs if q <= shortfall) / len(qs)


def note(s: Any, factors: dict[str, Any], text: str, ask: float, side: str, left: float) -> str:
    """The note one bet row carries (empty with no model yet). Never raises."""
    try:
        model = load(s)
        if model is None:
            return ""
        p = predict(model, readings(factors, text, ask, side, left))
        short = ask - p
        return f" [context score (shadow): wins {p:.2f} against the ask {ask:.2f}, shortfall {short:+.3f}, {percentile(model, short):.0f}th percentile of {model['n']} training bets]"
    except Exception:
        log.debug("context score: no note", exc_info=True)
        return ""


def annotate(d: dict[str, Any], s: Any) -> None:
    """Add the context score's note to a bet row about to be written (the row's factors snapshot is already in). Never raises."""
    try:
        close = d.get("close_time")
        left = 0.0
        if close:
            from datetime import datetime

            left = (datetime.fromisoformat(str(close).replace("Z", "+00:00")).timestamp() - float(d["ts"]))
        f = d.get("factors")
        factors = json.loads(f) if isinstance(f, str) and f else (f if isinstance(f, dict) else {})
        text = f"{d.get('context') or ''} {d.get('reason') or ''}"
        tag = note(s, factors, text, float(d["ask"]), str(d.get("side") or "yes"), left)
        if tag:
            d["reason"] = (str(d.get("reason") or "") + tag)[:1500]
    except Exception:
        log.debug("context score: not annotated", exc_info=True)


# ---- fitting (a low-priority process of its own; plain Python) ----
def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        d = m[c][c] or 1e-12
        for r in range(c + 1, n):
            f = m[r][c] / d
            if f:
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - sum(m[r][k] * x[k] for k in range(r + 1, n))) / (m[r][r] or 1e-12)
    return x


def fit(rows: list[tuple[dict[str, Any], int]], lam: float = LAMBDA, iters: int = 25) -> dict[str, Any] | None:
    """rows: (readings, won 0/1) for settled live bets. A ridge logistic regression for the win with the logit of the ask as the offset. None with under 100 rows."""
    if len(rows) < 100:
        return None
    feats = [*NUMERIC, "log_activity_60", "imbalance_x_side"]
    med: dict[str, float] = {}
    names: list[str] = []
    for k in feats:
        vals = sorted(float(r[k]) for r, _ in rows if r.get(k) is not None)
        if not vals:
            continue
        med[k] = vals[len(vals) // 2]
        names.append(k)
        if len(vals) < len(rows):
            names.append(k + "_missing")
    for col in CATEGORICAL:
        counts: dict[str, int] = {}
        for r, _ in rows:
            if r.get(col):
                counts[r[col]] = counts.get(r[col], 0) + 1
        for v in sorted(counts, key=lambda x: -counts[x])[:4]:
            names.append(f"{col}={v}")
    model: dict[str, Any] = {"features": names, "med": med}
    X = [_vector(model, r) for r, _ in rows]
    y = [w for _, w in rows]
    off = [math.log(min(max(float(r["ask"]), 1e-4), 1 - 1e-4) / (1 - min(max(float(r["ask"]), 1e-4), 1 - 1e-4))) for r, _ in rows]
    n, p = len(X), len(names)
    mu = [sum(row[j] for row in X) / n for j in range(p)]
    sd = [math.sqrt(sum((row[j] - mu[j]) ** 2 for row in X) / n) + 1e-9 for j in range(p)]
    Z = [[1.0] + [(row[j] - mu[j]) / sd[j] for j in range(p)] for row in X]
    b = [0.0] * (p + 1)
    for _ in range(iters):
        pr = [1.0 / (1.0 + math.exp(-(sum(bi * zi for bi, zi in zip(b, z, strict=True)) + o))) for z, o in zip(Z, off, strict=True)]
        w = [q * (1 - q) + 1e-9 for q in pr]
        g = [sum(z[j] * (yi - q) for z, yi, q in zip(Z, y, pr, strict=True)) - (lam * b[j] if j else 0.0) for j in range(p + 1)]
        H = [[sum(z[i] * z[j] * wi for z, wi in zip(Z, w, strict=True)) + (lam if i == j and i else 0.0) for j in range(p + 1)] for i in range(p + 1)]
        step = _solve(H, g)
        b = [bi + si for bi, si in zip(b, step, strict=True)]
        if max(abs(x) for x in step) < 1e-5:
            break
    model.update({"mu": mu, "sd": sd, "b": b, "n": n, "lam": lam, "built_at": time.time()})
    short = sorted(float(r["ask"]) - predict(model, r) for r, _ in rows)
    model["quantiles"] = [round(short[min(n - 1, int(i * n / 100))], 5) for i in range(100)]
    return model


def training_rows(s: Any) -> list[tuple[dict[str, Any], int]]:
    """Every settled live bet with a snapshot and a price, as (readings, won)."""
    from . import db

    st = db.connect(s.db_path, "main", shared=True)
    out = []
    for d in st.execute("SELECT d.ts, d.ask, d.side, d.factors, d.context, d.reason, w.result, w.close_ts FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.mode='trade' AND d.action='bet' AND d.lane<>'manual' AND d.stake_usd>0 AND d.ask>0 AND d.factors IS NOT NULL AND w.result IN ('yes','no')").fetchall():
        try:
            f = json.loads(d["factors"]) if isinstance(d["factors"], str) else d["factors"]
            if not isinstance(f, dict) or not f:
                continue
            text = f"{d['context'] or ''} {d['reason'] or ''}"
            out.append((readings(f, text, float(d["ask"]), d["side"], float(d["close_ts"]) - float(d["ts"])), 1 if d["result"] == d["side"] else 0))
        except (ValueError, TypeError):
            continue
    return out


def refit(s: Any) -> bool:
    model = fit(training_rows(s))
    if model is None:
        log.info("context score: too few settled live bets to fit")
        return False
    p = model_path(s)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(model))
    os.replace(tmp, p)
    log.info("context score: fitted on %d live bets", model["n"])
    return True


def maybe_refit(s: Any, now: float | None = None) -> bool:
    """Start the daily refit as its own low-priority process. True when one was started."""
    global _fitting
    now = time.time() if now is None else now
    if _fitting is not None and _fitting.poll() is None:
        return False
    m = load(s)
    if m and now - m["built_at"] < REFIT_EVERY:
        return False
    log.info("context score: refitting from the live bets (separate process)")
    _fitting = subprocess.Popen([sys.executable, "-m", "kalshi_moonshot.contextscore"], preexec_fn=lambda: os.nice(15))
    return True


if __name__ == "__main__":
    from . import config

    logging.basicConfig(level=logging.INFO)
    refit(config.load())
