"""Shadow study: add to a bet that is winning, late (2026-11-07, user: "once it passes 85c and the hour and 15m (55, 13 in Fibonacci) at under 120s left we should tack on another 2-5x the bet").

scripts/accumulator_study.py (studies/results/2026-11-07-accumulator.txt): on 1,829 settled crypto market sides, a buy at an ask of 0.85-0.99 inside the last 120 s paid -0.009 per $1 after the fee; with the cushion rising over both the 13 and the 55 minute look-backs (slope at least 0.5 typical moves) -0.007 (n=228, 9 losers); only a cushion over 1 typical move for the time left (z, n=198, about +0.009, 3 losers) was positive, a cut chosen after seeing the results.
This module runs the same read in shadow on every open bet of a lane with accumulate_shadow. It places no order. Per bet it logs once, the first time the held side's ask is in 0.85-0.99 with 5-120 s left: the ask, the cushion's z for the seconds left, the 13 and the 55 minute slopes. Nothing here is a coin or a market name; the readings are the spot's own curve.
The hourly score reads the settled bets and prints, per variant (price only, the user's slope rule, z over 1, both), the add-on's net per $1 after the full taker fee at 1x and 2x the first stake, and warns when a variant is negative over RETIRE_N signals in 7 days (CLAUDE.md rule 6).
Lines go to DATA_DIR/accumulator_shadow.jsonl: kind "signal" and kind "score"."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("moonshot")

LEFT_MAX, LEFT_MIN = 120.0, 5.0
ASK_MIN, ASK_MAX = 0.85, 0.99
LOOK13, LOOK55 = 13 * 60, 55 * 60
SLOPE_BAR = 0.5
Z_BAR = 1.0
EVERY = 5.0
SCORE_EVERY = 3600.0
RETIRE_N = 30
VARIANTS = ("price only", "slopes 13m+55m >= 0.5", "z >= 1", "slopes and z")

_lock = threading.Lock()
_seen: set[str] = set()
_ran_at = 0.0
_scored_at = 0.0


def readings(ts: list[float], vs: list[float], strike: float, sign: int, now: float, left: float) -> dict[str, float] | None:
    """The cushion's z for the seconds left and its slope over 13 and 55 minutes, from ticks. None when the history does not cover 55 minutes."""
    from . import curvemodel

    grid = curvemodel.live_grid(ts, vs, now)
    if grid is None or strike <= 0:
        return None
    g, sq, k = grid
    kk = math.log(strike)
    out: dict[str, float] = {}
    for name, look in (("s13", LOOK13), ("s55", LOOK55)):
        if k - look < 0 or g[k] is None or g[k - look] is None:
            return None
        rate = math.sqrt((sq[k] - sq[k - look]) / look)
        if rate <= 0:
            return None
        out[name] = ((g[k] - g[k - look]) * sign) / (rate * math.sqrt(look))
        if name == "s13":
            out["z"] = (g[k] - kk) * sign / (rate * math.sqrt(max(left, 1.0)))
    return out


def picks(sig: dict[str, Any]) -> dict[str, bool]:
    slopes = sig["s13"] >= SLOPE_BAR and sig["s55"] >= SLOPE_BAR
    z = sig["z"] >= Z_BAR
    return {"price only": True, "slopes 13m+55m >= 0.5": slopes, "z >= 1": z, "slopes and z": slopes and z}


def shadow_pass(s: Any, store: Any, tape: Any, specs: dict[str, Any], strike_of: Any, now: float | None = None) -> list[dict[str, Any]]:
    """Look at each open bet of an accumulate_shadow lane inside its last 120 s and log the first moment its side's ask is 0.85-0.99. strike_of(ticker) gives the market's strike. Returns the new rows."""
    global _ran_at
    from . import ticks as ticks_mod
    from . import variants

    now = time.time() if now is None else now
    if now - _ran_at < EVERY or tape is None:
        return []
    _ran_at = now
    path = Path(s.data_dir) / "accumulator_shadow.jsonl"
    if not _seen:
        try:
            for line in path.read_text().splitlines():
                r = json.loads(line)
                if r.get("kind") == "signal" and now - r["ts"] < 86400.0:
                    _seen.add(f"{r['ticker']}|{r['lane']}")
        except (OSError, ValueError, KeyError):
            pass
    out: list[dict[str, Any]] = []
    for row in store.open_bets():
        lane, ticker = row["lane"] or "moonshot", row["ticker"]
        spec = specs.get(lane)
        key = f"{ticker}|{lane}"
        if spec is None or not getattr(spec, "accumulate_shadow", False) or key in _seen or not row["close_time"] or not ticker.split("-")[0].endswith("15M"):
            continue
        try:
            left = datetime.fromisoformat(str(row["close_time"])).timestamp() - now
        except (TypeError, ValueError):
            continue
        if not (LEFT_MIN <= left <= LEFT_MAX):
            continue
        q = tape.execute("SELECT bid, ask, ts FROM quotes WHERE ticker=? ORDER BY ts DESC LIMIT 1", (ticker,)).fetchone()
        if not q or q["bid"] is None or q["ask"] is None or now - float(q["ts"]) > 20.0:
            continue
        side = row["side"] or "yes"
        ask = float(q["ask"]) if side == "yes" else 1.0 - float(q["bid"])
        if not (ASK_MIN <= ask <= ASK_MAX):
            continue
        idx = variants.crypto_index(ticker.split("-")[0])
        got = ticks_mod.history.window(idx, now - LOOK55 - 40.0, now) if idx else None
        try:
            strike = float(strike_of(ticker) or 0.0)
        except (TypeError, ValueError):
            strike = 0.0
        if not got or not got[0] or strike <= 0:
            continue
        r = readings(got[0], got[1], strike, 1 if side == "yes" else -1, now, left)
        if r is None:
            continue
        _seen.add(key)
        out.append({"kind": "signal", "ts": round(now, 1), "ticker": ticker, "lane": lane, "side": side, "left": round(left, 1), "ask": round(ask, 4), "z": round(r["z"], 3), "s13": round(r["s13"], 3), "s55": round(r["s55"], 3)})
    if out:
        try:
            with path.open("a") as fh:
                for line in out:
                    fh.write(json.dumps(line) + "\n")
        except OSError:
            log.debug("accumulator shadow: could not write %s", path, exc_info=True)
        for line in out:
            log.info("accumulator shadow: %s %s %s, %.0fs left, ask %.2f, z %.2f, slope 13m %.2f, 55m %.2f: would add on", line["lane"], line["ticker"], line["side"], line["left"], line["ask"], line["z"], line["s13"], line["s55"])
    return out


def score_signal(sig: dict[str, Any], outcome: str) -> float:
    """The add-on's net per $1 staked after the full taker fee, held to the result."""
    from .strategy import fee_per_contract

    ask = sig["ask"]
    c = 1.0 / ask
    return ((1.0 - ask) * c if outcome == sig["side"] else -1.0) - fee_per_contract(ask) * c


def run_score(s: Any, store: Any, now: float | None = None) -> list[dict[str, Any]]:
    """Hourly: score the signals whose market has settled and log the 7-day totals per variant; warn on a variant that is negative over RETIRE_N signals."""
    global _scored_at
    now = time.time() if now is None else now
    if now - _scored_at < SCORE_EVERY:
        return []
    _scored_at = now
    path = Path(s.data_dir) / "accumulator_shadow.jsonl"
    try:
        rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        return []
    done = {(r["ticker"], r["lane"]) for r in rows if r.get("kind") == "score"}
    new: list[dict[str, Any]] = []
    for sig in rows:
        if sig.get("kind") != "signal" or (sig["ticker"], sig["lane"]) in done:
            continue
        bet = store.db.execute("SELECT outcome FROM decisions WHERE action='bet' AND ticker=? AND COALESCE(lane,'moonshot')=? ORDER BY id LIMIT 1", (sig["ticker"], sig["lane"])).fetchone()
        if not bet or bet["outcome"] not in ("yes", "no"):
            continue
        new.append({"kind": "score", "ts": sig["ts"], "ticker": sig["ticker"], "lane": sig["lane"], "net": round(score_signal(sig, bet["outcome"]), 4), "picks": picks(sig)})
    if new:
        try:
            with path.open("a") as fh:
                for line in new:
                    fh.write(json.dumps(line) + "\n")
        except OSError:
            log.debug("accumulator shadow: could not write the scores", exc_info=True)
    recent = [r for r in rows + new if r.get("kind") == "score" and now - r["ts"] <= 7 * 86400.0]
    summary: dict[str, tuple[int, float]] = {}
    for v in VARIANTS:
        pick = [r for r in recent if r["picks"].get(v)]
        summary[v] = (len(pick), round(sum(r["net"] for r in pick), 3))
        if len(pick) >= RETIRE_N and sum(r["net"] for r in pick) < 0:
            log.warning("accumulator shadow: '%s' is negative over %d signals in 7 days (%.2f per $1 summed): retire it", v, len(pick), sum(r["net"] for r in pick))
    log.info("accumulator shadow (7 d, variant: signals, add-on net per $1 summed; times the add-on multiple for dollars per $1 of first stake): %s", summary)
    return new
