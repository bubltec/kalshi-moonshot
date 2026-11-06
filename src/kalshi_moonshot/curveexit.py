"""Shadow study: sell on a bounce and buy the other side, read from the curve (2026-11-06).

Replay on 1,465 settled live bets (scripts/curve_flip_replay.py, studies/results/2026-11-06-curve-flip.txt): in the last five minutes, when the spot has bounced BOUNCE typical minutes off its best level for the bet's side within the last five minutes, selling the bet at the bid and buying the other side at 1 - that bid paid when the spot's path over those five minutes was choppy (low efficiency: +$152 over 111 signals), was mixed in the middle (+$26) and lost when it was straight (-$72). The choppy gain held for crypto-class and commodity-class markets and in both halves of the eight days; the cutoffs and the bounce size were chosen after seeing the results.
This module runs the same rule in shadow on every open bet of a lane with curve_shadow set. It places no order. Per bet it logs, once, the first time the bounce reaches BOUNCE with 5-300 s left: the curve reading and its state, the bid, what selling would return and what the other side would cost. A signal is scored when the market settles: the sale plus the other-side leg against what the bet actually returned, fees on every leg.
Everything is read from the curve (CLAUDE.md): the bounce is in typical minutes (the root-mean-square tick move over the last 900 s times sqrt(60)), efficiency is net move over path length, and the choppy/straight cutoffs are the 33rd/67th percentiles of that market's own readings over the last hours, recomputed hourly. A reading outside the market's own 1st-99th percentile of efficiency, or a typical minute over three times its median, is state "anomaly": it is logged and never counted as choppy or straight.
Lines go to DATA_DIR/curve_shadow.jsonl: kind "signal" and kind "score". Nothing here changes a lane, a stake or an order."""

from __future__ import annotations

import bisect
import itertools
import json
import logging
import math
import statistics
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("moonshot")

BOUNCE = 3.0  # typical minutes off the best level (structural: it is in the market's own variance units)
SPAN = 300.0  # seconds of spot the bounce and the efficiency are read over
LEFT_MAX, LEFT_MIN = 300.0, 5.0
EVERY = 5.0
SCORE_EVERY = 3600.0
CUT_HOURS = (12.0, 6.0, 3.0, 1.0)  # the longest look-back the tick history covers is used
MIN_SAMPLES = 60
ANOMALY_UNIT = 3.0
CHOPPY_Q, STRAIGHT_Q = 1 / 3, 2 / 3
RETIRE_N = 30

_lock = threading.Lock()
_cuts: dict[str, dict[str, float]] = {}
_cuts_at: dict[str, float] = {}
_seen: set[str] = set()
_ran_at = 0.0
_scored_at = 0.0


def _unit(ts: list[float], vs: list[float]) -> float | None:
    steps = [math.log(b / a) for a, b in itertools.pairwise(vs) if a > 0 and b > 0]
    return math.sqrt(sum(x * x for x in steps) / len(steps)) * math.sqrt(60.0) if len(steps) >= 30 else None


def readings(ts: list[float], vs: list[float], side: str, at: float, span: float = SPAN) -> dict[str, float] | None:
    """The curve at `at`: bounce (typical minutes the spot has moved off its best level for `side` within `span`: down from the high for YES, up from the low for NO), efficiency (net move over path length across `span`), and the typical minute (from the 900 s before). None when the history is too short or stale."""
    j = bisect.bisect_right(ts, at) - 1
    k = bisect.bisect_right(ts, at - span) - 1
    if j < 0 or k < 0 or at - ts[j] > 10.0:
        return None
    lo = bisect.bisect_right(ts, at - 900.0)
    unit = _unit(ts[lo : j + 1], vs[lo : j + 1])
    if not unit:
        return None
    win = vs[k : j + 1]
    if len(win) < 30:
        return None
    path = sum(abs(math.log(b / a)) for a, b in itertools.pairwise(win) if a > 0 and b > 0)
    eff = abs(math.log(win[-1] / win[0])) / path if path > 0 else 0.0
    bounce = math.log(max(win) / vs[j]) / unit if side == "yes" else math.log(vs[j] / min(win)) / unit
    return {"bounce": bounce, "eff": eff, "unit": unit}


def cuts_from(ts: list[float], vs: list[float], now: float) -> dict[str, float] | None:
    """The market's own cutoffs from its recent readings: one efficiency and one typical minute per minute of history. Efficiency 1st/33rd/67th/99th percentiles and the median typical minute. None when too few readings."""
    effs: list[float] = []
    units: list[float] = []
    t = ts[0] + SPAN + 60.0 if ts else now
    while t <= min(now, ts[-1] if ts else 0.0):
        r = readings(ts, vs, "yes", t)
        if r:
            effs.append(r["eff"])
            units.append(r["unit"])
        t += 60.0
    if len(effs) < MIN_SAMPLES:
        return None
    effs.sort()

    def q(p: float) -> float:
        return effs[min(len(effs) - 1, int(p * len(effs)))]

    return {"p01": q(0.01), "p33": q(CHOPPY_Q), "p67": q(STRAIGHT_Q), "p99": q(0.99), "unit_med": statistics.median(units), "n": float(len(effs))}


def state(r: dict[str, float], cuts: dict[str, float] | None) -> str:
    """choppy / middle / straight against the market's own cutoffs; anomaly when outside its own recent range; unknown without cutoffs."""
    if cuts is None:
        return "unknown"
    if r["eff"] < cuts["p01"] or r["eff"] > cuts["p99"] or r["unit"] > ANOMALY_UNIT * cuts["unit_med"]:
        return "anomaly"
    return "choppy" if r["eff"] < cuts["p33"] else ("straight" if r["eff"] >= cuts["p67"] else "middle")


def _cuts_for(index_id: str, now: float) -> dict[str, float] | None:
    from . import ticks as ticks_mod

    with _lock:
        if now - _cuts_at.get(index_id, 0.0) < 3600.0:
            return _cuts.get(index_id)
    got = None
    for h in CUT_HOURS:
        got = ticks_mod.history.window(index_id, now - h * 3600.0, now)
        if got:
            break
    c = cuts_from(got[0], got[1], now) if got else None
    with _lock:
        _cuts_at[index_id] = now
        if c:
            _cuts[index_id] = c
    return c


def _load_seen(path: Path, now: float) -> None:
    try:
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if row.get("kind") == "signal" and now - row["ts"] < 86400.0:
                _seen.add(f"{row['ticker']}|{row['lane']}")
    except (OSError, ValueError, KeyError):
        pass


def shadow_pass(s: Any, store: Any, tape: Any, specs: dict[str, Any], now: float | None = None) -> list[dict[str, Any]]:
    """Look at each open bet of a curve_shadow lane inside its last five minutes and log the first bounce signal. Returns the new rows."""
    global _ran_at
    from . import marketprofile
    from . import ticks as ticks_mod

    now = time.time() if now is None else now
    if now - _ran_at < EVERY or tape is None:
        return []
    _ran_at = now
    path = Path(s.data_dir) / "curve_shadow.jsonl"
    if not _seen:
        _load_seen(path, now)
    out: list[dict[str, Any]] = []
    for row in store.open_bets():
        spec = specs.get(row["lane"] or "moonshot")
        ticker, lane = row["ticker"], row["lane"] or "moonshot"
        key = f"{ticker}|{lane}"
        if spec is None or not spec.curve_shadow or key in _seen or not row["close_time"] or not row["contracts"]:
            continue
        try:
            left = datetime.fromisoformat(str(row["close_time"])).timestamp() - now
        except (TypeError, ValueError):
            continue
        if not (LEFT_MIN <= left <= LEFT_MAX):
            continue
        idx = marketprofile._index(ticker.split("-")[0])
        got = ticks_mod.history.window(idx, now - 1500.0, now) if idx else None
        if not got:
            continue
        side = row["side"] or "yes"
        r = readings(got[0], got[1], side, now)
        if r is None or r["bounce"] < BOUNCE:
            continue
        q = tape.execute("SELECT bid, ask, ts FROM quotes WHERE ticker=? ORDER BY ts DESC LIMIT 1", (ticker,)).fetchone()
        if not q or q["bid"] is None or q["ask"] is None or now - float(q["ts"]) > 20.0:
            continue
        bid = float(q["bid"]) if side == "yes" else 1.0 - float(q["ask"])
        cuts = _cuts_for(idx, now)
        _seen.add(key)
        out.append({
            "kind": "signal", "ts": round(now, 1), "ticker": ticker, "lane": lane, "side": side, "left": round(left, 1), "entry": round(float(row["stake_usd"] or 0.0) / float(row["contracts"]), 4),
            "contracts": float(row["contracts"]), "stake": float(row["stake_usd"] or 0.0), "bid": round(bid, 4), "other_ask": round(1.0 - bid, 4), "state": state(r, cuts),
            "bounce": round(r["bounce"], 2), "eff": round(r["eff"], 4), "unit": round(r["unit"] * 1e4, 2), "p33": round(cuts["p33"], 4) if cuts else None, "p67": round(cuts["p67"], 4) if cuts else None,
        })
    if out:
        try:
            with path.open("a") as fh:
                for line in out:
                    fh.write(json.dumps(line) + "\n")
        except OSError:
            log.debug("curve shadow: could not write %s", path, exc_info=True)
        for line in out:
            log.info("curve shadow: %s %s %s, %.0fs left, bounce %.1f, eff %.3f, %s: would sell at %.2f and buy the other side at %.2f", line["lane"], line["ticker"], line["side"], line["left"], line["bounce"], line["eff"], line["state"], line["bid"], line["other_ask"])
    return out


def score_signal(sig: dict[str, Any], actual: float, other_won: bool) -> dict[str, float]:
    """Dollars for one signal against what the bet actually returned (profit): the sale alone, and the sale plus an equal-size other-side leg, fees on each leg."""
    from .strategy import fee_per_contract

    n, bid, ob = sig["contracts"], sig["bid"], sig["other_ask"]
    sell = n * bid - fee_per_contract(bid) * n - sig["stake"]
    flip = n * (1.0 if other_won else 0.0) - n * ob - fee_per_contract(ob) * n if 0.02 <= ob <= 0.98 else 0.0
    return {"actual": round(actual, 4), "sell": round(sell, 4), "sell_change": round(sell - actual, 4), "flip_change": round(sell + flip - actual, 4)}


def run_score(s: Any, store: Any, now: float | None = None) -> list[dict[str, Any]]:
    """Hourly: score the signals whose market has settled, log the totals by state, and flag a state whose flip result turns negative."""
    global _scored_at
    from .marketreview import bet_return

    now = time.time() if now is None else now
    if now - _scored_at < SCORE_EVERY:
        return []
    _scored_at = now
    path = Path(s.data_dir) / "curve_shadow.jsonl"
    try:
        rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        return []
    done = {(r["ticker"], r["lane"]) for r in rows if r.get("kind") == "score"}
    new: list[dict[str, Any]] = []
    for sig in rows:
        if sig.get("kind") != "signal" or (sig["ticker"], sig["lane"]) in done:
            continue
        bet = store.db.execute(
            "SELECT ts, ticker, side, ask, contracts, stake_usd, outcome, exit_price FROM decisions WHERE action='bet' AND ticker=? AND COALESCE(lane,'moonshot')=? ORDER BY id LIMIT 1", (sig["ticker"], sig["lane"])
        ).fetchone()
        if not bet or bet["outcome"] not in ("yes", "no"):
            continue
        got = bet_return(bet)
        if got is None:
            continue
        new.append({"kind": "score", "ts": sig["ts"], "ticker": sig["ticker"], "lane": sig["lane"], "state": sig["state"], **score_signal(sig, got[1], bet["outcome"] != sig["side"])})
    if new:
        try:
            with path.open("a") as fh:
                for line in new:
                    fh.write(json.dumps(line) + "\n")
        except OSError:
            log.debug("curve shadow: could not write the scores", exc_info=True)
    allscores = [r for r in rows + new if r.get("kind") == "score" and now - r["ts"] <= 7 * 86400.0]
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in allscores:
        by[r["state"]].append(r)
    summary = {k: (len(v), round(sum(x["sell_change"] for x in v), 2), round(sum(x["flip_change"] for x in v), 2)) for k, v in sorted(by.items())}
    log.info("curve shadow (7 d, state: signals, sell-only change, sell+flip change): %s", summary)
    choppy = by.get("choppy", [])
    if len(choppy) >= RETIRE_N and sum(x["flip_change"] for x in choppy) < 0:
        log.warning("curve shadow: the choppy-state sell+flip rule is negative over %d signals in 7 days (%.2f): retire it", len(choppy), sum(x["flip_change"] for x in choppy))
    return new
