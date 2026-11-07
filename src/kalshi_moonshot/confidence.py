"""Confidence score in shadow (2026-11-06): the chance a crypto 15-minute market settles YES, read from the curve and compared with the market price.

scripts/confidence_study.py (studies/results/2026-11-06-confidence-study.txt): a logistic model on logit(mid) plus the curve (distance to the strike in typical minutes, 60 s drift, bounce off the 5-minute extremes, path efficiency, 15-minute range and position, the hour's move, where the strike and the spot sit in the hour's range, time left) beat the price by almost nothing on average, but its buys where it beat the ask by 3-6 cents were positive in both halves of three test days. Not established. This runs the same model live, in shadow, to find out.
Every distance is in the market's own typical minutes (CLAUDE.md). The model is refit daily from the tape on the trailing days, in a separate low-priority process, so it follows the market's current behaviour.
Per open bet of a lane with confidence_shadow set, at the buy (mark 0) and at about 300, 180 and 90 s left, one row is logged: the model's chance for the bet's side against that side's ask and bid. When the market settles each row is scored: what buying more at the ask returned (after the fee), and what selling at the bid gained over holding. Rows go to DATA_DIR/confidence_shadow.jsonl; the model to DATA_DIR/confidence_model.json. Nothing here changes a lane, a stake or an order."""

from __future__ import annotations

import array
import bisect
import itertools
import json
import logging
import math
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("moonshot")

MARKS = (300, 180, 90)
MARK_SLACK = 6.0
ENTRY_WITHIN = 90.0  # a bet seen within this many seconds of its buy gets an "entry" reading (mark 0)
EVERY = 5.0
SCORE_EVERY = 3600.0
REFIT_EVERY = 86400.0
TRAIN_DAYS = 9.0
RIDGE = 1.0
MIN_ROWS = 1500
NAMES = ["z_tm", "d60", "up5", "dn5", "eff5", "amp15", "pos15", "h_move", "h_pos", "h_strike", "left"]
EDGES = (0.03, 0.06)

_ran_at = 0.0
_scored_at = 0.0
_seen: set[str] = set()
_model: dict[str, Any] | None = None
_model_mtime = 0.0
_fitting: subprocess.Popen[bytes] | None = None


def logit(p: float) -> float:
    p = min(1 - 1e-4, max(1e-4, p))
    return math.log(p / (1 - p))


def features_at(T: Any, V: Any, t: float, strike: float, left: float, bid: float, ask: float) -> dict[str, float] | None:
    """The curve at time t for a market with this strike, from sorted tick times T and values V (at least an hour of history before t), and the quote. None when the history is too short or stale. Same code trains and scores."""
    j = bisect.bisect_right(T, t) - 1
    lo900, lo300, lo60, lo3600 = (bisect.bisect_right(T, t - x) for x in (900, 300, 60, 3600))
    if j < 0 or t - T[j] > 10 or j - lo900 < 600 or lo3600 >= j or T[lo3600] < t - 3700 or not (0 < ask <= 1) or bid < 0 or strike <= 0 or left <= 0:
        return None
    seq = V[lo900 : j + 1]
    steps = [math.log(b / a) for a, b in itertools.pairwise(seq) if a > 0 and b > 0]
    if len(steps) < 300:
        return None
    sig1 = math.sqrt(sum(x * x for x in steps) / len(steps))
    unit = sig1 * math.sqrt(60.0)
    if unit <= 0:
        return None
    spot = V[j]
    w5, h = V[lo300 : j + 1], V[lo3600 : j + 1]
    lo15, hi15, lo_h, hi_h = min(seq), max(seq), min(h), max(h)
    path5 = sum(abs(math.log(b / a)) for a, b in itertools.pairwise(w5) if a > 0 and b > 0)
    return {
        "logit_mid": logit((ask + bid) / 2.0),
        "z_tm": math.log(spot / strike) / unit,
        "d60": math.log(spot / V[lo60]) / unit if lo60 <= j else 0.0,
        "up5": math.log(max(w5) / spot) / unit,
        "dn5": math.log(spot / min(w5)) / unit,
        "eff5": (abs(math.log(w5[-1] / w5[0])) / path5) if path5 > 0 else 0.0,
        "amp15": math.log(hi15 / lo15) / unit,
        "pos15": (spot - lo15) / (hi15 - lo15) if hi15 > lo15 else 0.5,
        "h_move": math.log(spot / h[0]) / unit,
        "h_pos": (spot - lo_h) / (hi_h - lo_h) if hi_h > lo_h else 0.5,
        "h_strike": (strike - lo_h) / (hi_h - lo_h) if hi_h > lo_h else 0.5,
        "left": left / 60.0,
    }


def vector(f: dict[str, float], mu: dict[str, float], sd: dict[str, float]) -> list[float]:
    return [1.0, f["logit_mid"]] + [(f[n] - mu[n]) / sd[n] for n in NAMES]


def fit(X: list[list[float]], y: list[int], ridge: float = RIDGE, iters: int = 12) -> list[float]:
    """Ridge logistic regression by Newton's method (pure Python: the host has no numpy). The first weight (the constant) is not penalised."""
    k = len(X[0])
    w = [0.0] * k
    for _ in range(iters):
        g = [0.0] * k
        H = [[0.0] * k for _ in range(k)]
        for xi, yi in zip(X, y, strict=True):
            z = sum(a * b for a, b in zip(w, xi, strict=True))
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
            wt = p * (1 - p) + 1e-6
            r = p - yi
            for a in range(k):
                g[a] += r * xi[a]
                xa = wt * xi[a]
                Ha = H[a]
                for b in range(a, k):
                    Ha[b] += xa * xi[b]
        for a in range(k):
            for b in range(a):
                H[a][b] = H[b][a]
            if a > 0:
                H[a][a] += ridge
                g[a] += ridge * w[a]
        A = [[*H[a], g[a]] for a in range(k)]
        for c in range(k):
            piv = max(range(c, k), key=lambda q: abs(A[q][c]))
            A[c], A[piv] = A[piv], A[c]
            pv = A[c][c] or 1e-9
            A[c] = [x / pv for x in A[c]]
            for q in range(k):
                if q != c and A[q][c]:
                    fac = A[q][c]
                    A[q] = [x - fac * z for x, z in zip(A[q], A[c], strict=True)]
        dw = [A[a][k] for a in range(k)]
        w = [a - b for a, b in zip(w, dw, strict=True)]
        if max(abs(x) for x in dw) < 1e-6:
            break
    return w


def predict(w: list[float], x: list[float]) -> float:
    z = sum(a * b for a, b in zip(w, x, strict=True))
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def train(s: Any, now: float | None = None) -> dict[str, Any] | None:
    """Build the rows from the tape (settled crypto windows with a strike, one row per mark), fit, and write the model file. Runs in its own process."""
    from . import db
    from .kalshiws import index_for_series
    from .store import Store

    now = time.time() if now is None else now
    tape = db.connect(s.tape_path, "tape", shared=True)
    store = Store(s.db_path, s.exclude_versions)
    wins = store.db.execute(
        "SELECT ticker, series, close_ts, result, strike FROM windows WHERE result IN ('yes','no') AND strike IS NOT NULL AND close_ts>? ORDER BY close_ts", (now - TRAIN_DAYS * 86400,)
    ).fetchall()
    rows: list[tuple[float, int, dict[str, float]]] = []
    for ser in sorted({w["series"] for w in wins if w["series"] and w["series"].startswith("KX") and w["series"][2:5] in ("BTC", "ETH", "SOL", "XRP", "DOG")}):
        idx = index_for_series(ser)
        if not idx:
            continue
        d = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (idx, now - TRAIN_DAYS * 86400 - 4500)).fetchall()
        T = array.array("d", (float(r[0]) for r in d))
        V = array.array("d", (float(r[1]) for r in d))
        del d
        for w in (x for x in wins if x["series"] == ser):
            close = float(w["close_ts"])
            quotes = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 400, close)).fetchall()
            qt = [float(q["ts"]) for q in quotes]
            for left in MARKS:
                t = close - left
                qi = bisect.bisect_right(qt, t) - 1
                if qi < 0 or t - qt[qi] > 15 or quotes[qi]["bid"] is None or quotes[qi]["ask"] is None:
                    continue
                f = features_at(T, V, t, float(w["strike"]), left, float(quotes[qi]["bid"]), float(quotes[qi]["ask"]))
                if f:
                    rows.append((close, 1 if w["result"] == "yes" else 0, f))
    if len(rows) < MIN_ROWS:
        log.warning("confidence: only %d rows, not fitting", len(rows))
        return None
    mu = {n: sum(r[2][n] for r in rows) / len(rows) for n in NAMES}
    sd = {n: (sum((r[2][n] - mu[n]) ** 2 for r in rows) / len(rows)) ** 0.5 or 1.0 for n in NAMES}
    X = [vector(r[2], mu, sd) for r in rows]
    y = [r[1] for r in rows]
    w = fit(X, y)
    ps = [predict(w, x) for x in X]
    ll = -sum(math.log(max(1e-6, p if yy else 1 - p)) for p, yy in zip(ps, y, strict=True)) / len(y)
    model = {"fitted_at": now, "n": len(rows), "days": TRAIN_DAYS, "mu": mu, "sd": sd, "w": w, "names": ["const", "logit_mid", *NAMES], "train_logloss": round(ll, 4)}
    tmp = Path(s.data_dir) / "confidence_model.json.tmp"
    tmp.write_text(json.dumps(model))
    tmp.replace(Path(s.data_dir) / "confidence_model.json")
    return model


def load_model(s: Any) -> dict[str, Any] | None:
    global _model, _model_mtime
    p = Path(s.data_dir) / "confidence_model.json"
    try:
        m = p.stat().st_mtime
        if _model is None or m != _model_mtime:
            _model, _model_mtime = json.loads(p.read_text()), m
    except (OSError, ValueError):
        return _model
    return _model


def maybe_refit(s: Any, now: float | None = None) -> bool:
    """Start the daily refit as a separate low-priority process (its own interpreter, so the watcher is not slowed). True when one was started."""
    global _fitting
    now = time.time() if now is None else now
    if _fitting is not None and _fitting.poll() is None:
        return False
    model = load_model(s)
    if model and now - model["fitted_at"] < REFIT_EVERY:
        return False
    log.info("confidence: refitting the model from the tape (separate process)")
    _fitting = subprocess.Popen([sys.executable, "-m", "kalshi_moonshot.confidence", "fit"], preexec_fn=lambda: os.nice(15))  # lowers the child's priority only
    return True


def shadow_pass(s: Any, store: Any, tape: Any, specs: dict[str, Any], strike_of: Any, now: float | None = None) -> list[dict[str, Any]]:
    """Log the model's chance for each open bet of a confidence_shadow lane at its marks. strike_of(ticker) gives the market's strike. Returns the new rows."""
    global _ran_at
    from . import marketprofile
    from . import ticks as ticks_mod

    now = time.time() if now is None else now
    if now - _ran_at < EVERY or tape is None:
        return []
    _ran_at = now
    model = load_model(s)
    if model is None:
        return []
    path = Path(s.data_dir) / "confidence_shadow.jsonl"
    if not _seen:
        try:
            for line in path.read_text().splitlines():
                r = json.loads(line)
                if r.get("kind") == "signal" and now - r["ts"] < 86400.0:
                    _seen.add(f"{r['ticker']}|{r['lane']}|{r['mark']}")
        except (OSError, ValueError, KeyError):
            pass
    out: list[dict[str, Any]] = []
    for row in store.open_bets():
        lane, ticker = row["lane"] or "moonshot", row["ticker"]
        spec = specs.get(lane)
        if spec is None or not spec.confidence_shadow or not row["close_time"] or not ticker.split("-")[0].endswith("15M"):
            continue
        try:
            left = datetime.fromisoformat(str(row["close_time"])).timestamp() - now
        except (TypeError, ValueError):
            continue
        todo = []
        if now - float(row["ts"] or 0.0) <= ENTRY_WITHIN and f"{ticker}|{lane}|0" not in _seen:
            todo.append(0)  # the reading at the buy itself, to set against the readings after it
        near = next((m for m in MARKS if abs(left - m) <= MARK_SLACK), None)
        if near is not None and f"{ticker}|{lane}|{near}" not in _seen:
            todo.append(near)
        if not todo:
            continue
        idx = marketprofile._index(ticker.split("-")[0])
        got = ticks_mod.history.window(idx, now - 3750.0, now) if idx else None
        q = tape.execute("SELECT bid, ask, ts FROM quotes WHERE ticker=? ORDER BY ts DESC LIMIT 1", (ticker,)).fetchone()
        if not got or not q or q["bid"] is None or q["ask"] is None or now - float(q["ts"]) > 20.0:
            continue
        try:
            strike = float(strike_of(ticker))
        except (TypeError, ValueError, KeyError):
            continue
        f = features_at(got[0], got[1], now, strike, left, float(q["bid"]), float(q["ask"]))
        if f is None:
            continue
        p_yes = predict(model["w"], vector(f, model["mu"], model["sd"]))
        side = row["side"] or "yes"
        yb, ya = float(q["bid"]), float(q["ask"])
        p_side, ask_side, bid_side = (p_yes, ya, yb) if side == "yes" else (1.0 - p_yes, 1.0 - yb, 1.0 - ya)
        for mark in todo:
            _seen.add(f"{ticker}|{lane}|{mark}")
            out.append({
                "kind": "signal", "ts": round(now, 1), "ticker": ticker, "lane": lane, "side": side, "mark": mark, "left": round(left, 1), "p_side": round(p_side, 4), "ask_side": round(ask_side, 4),
                "bid_side": round(bid_side, 4), "edge_buy": round(p_side - ask_side, 4), "edge_sell": round(bid_side - p_side, 4), "model_at": model["fitted_at"], "z_tm": round(f["z_tm"], 2),
            })
    if out:
        try:
            with path.open("a") as fh:
                for line in out:
                    fh.write(json.dumps(line) + "\n")
        except OSError:
            log.debug("confidence: could not write %s", path, exc_info=True)
        for r in out:
            log.info("confidence shadow: %s %s %s %s: model %.2f for the side, ask %.2f, bid %.2f (buy edge %+.2f, sell edge %+.2f)", r["lane"], r["ticker"], r["side"], "at entry" if r["mark"] == 0 else f"at {r['mark']}s left", r["p_side"], r["ask_side"], r["bid_side"], r["edge_buy"], r["edge_sell"])
    return out


def score_row(sig: dict[str, Any], won: bool) -> dict[str, float]:
    """One signal against the result: what buying one more contract at the ask returned (after the fee), and what selling at the bid gained over holding."""
    from .strategy import fee_per_contract

    ask, bid = sig["ask_side"], sig["bid_side"]
    win = 1.0 if won else 0.0
    return {"buy_ev": round(win - ask - fee_per_contract(ask), 4), "sell_gain": round(bid - fee_per_contract(bid) - win, 4), "p_error": round((sig["p_side"] - win) ** 2, 5), "ask_error": round((ask - win) ** 2, 5)}


def run_score(s: Any, store: Any, now: float | None = None) -> list[dict[str, Any]]:
    """Hourly: score the signals whose market has settled and log the totals: Brier of the model against the ask, and the buy and sell results by edge."""
    global _scored_at
    now = time.time() if now is None else now
    if now - _scored_at < SCORE_EVERY:
        return []
    _scored_at = now
    path = Path(s.data_dir) / "confidence_shadow.jsonl"
    try:
        rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        return []
    done = {(r["ticker"], r["lane"], r["mark"]) for r in rows if r.get("kind") == "score"}
    new: list[dict[str, Any]] = []
    for sig in rows:
        if sig.get("kind") != "signal" or (sig["ticker"], sig["lane"], sig["mark"]) in done:
            continue
        got = store.db.execute("SELECT outcome FROM decisions WHERE action='bet' AND ticker=? AND outcome IN ('yes','no') ORDER BY id LIMIT 1", (sig["ticker"],)).fetchone()
        if not got:
            continue
        new.append({"kind": "score", **{k: sig[k] for k in ("ts", "ticker", "lane", "mark", "edge_buy", "edge_sell")}, **score_row(sig, got["outcome"] == sig["side"])})
    if new:
        try:
            with path.open("a") as fh:
                for line in new:
                    fh.write(json.dumps(line) + "\n")
        except OSError:
            log.debug("confidence: could not write the scores", exc_info=True)
    sc = [r for r in rows + new if r.get("kind") == "score" and now - r["ts"] <= 7 * 86400.0]
    if sc:
        parts = [f"{len(sc)} scored: Brier model {sum(r['p_error'] for r in sc) / len(sc):.4f} vs ask {sum(r['ask_error'] for r in sc) / len(sc):.4f}"]
        for e in EDGES:
            b = [r for r in sc if r["edge_buy"] >= e]
            m = [r for r in sc if r["edge_sell"] >= e]
            parts.append(f"buy edge>={e:.2f}: {len(b)} EV {sum(r['buy_ev'] for r in b) / max(len(b), 1):+.3f}; sell edge>={e:.2f}: {len(m)} gain {sum(r['sell_gain'] for r in m) / max(len(m), 1):+.3f}")
        log.info("confidence shadow (7 d): %s", " | ".join(parts))
    return new


if __name__ == "__main__":
    from . import config

    logging.basicConfig(level=logging.INFO)
    m = train(config.reload())
    print("fitted" if m else "not fitted", (m or {}).get("n"), (m or {}).get("train_logloss"))
