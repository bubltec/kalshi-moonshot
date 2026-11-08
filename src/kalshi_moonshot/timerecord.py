"""The lane's own record by minutes into the window (2026-11-08, user: "the problem isn't buying a low price, it's when you buy it").

studies/results/2026-11-08-multiscale-by-elapsed.txt and the 168 settled crypto_multiscale bets: bought 2-4 minutes in, 21 of 36 won and the bets netted +$23.41 (both halves positive); bought 9-15 minutes in, 8 of 46 won and they netted -$23.52 (both halves negative). A fixed minute cutoff would be a number picked by hand, so the lane keeps its own score by time instead (CLAUDE.md rules 2, 6, 8).
Per lane and per two-minute bucket of the 15-minute window, from the lane's settled live bets of the last 7 days: the net in dollars over the dollars staked, shrunk toward zero by SHRINK dollars so a few bets cannot decide it. A bucket with at least MIN_N bets and a negative score is a bucket the lane sits out; a bucket without that evidence is open. Recomputed hourly.
Every model_buy lane runs this in shadow: each buy the lane's other rules clear is logged to DATA_DIR/timing_shadow.jsonl with the bucket's score and whether the record would have skipped it, and scored hourly against the settled window (net per $1 after the full taker fee, held to the result), kept against would-skip. The lane skips for real only with timing_gate = true (default off, the live switch is the user's). A would-skip subset that is positive over RETIRE_N signals in 7 days is flagged: the gate would have cost money (rule 6).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("moonshot")

BUCKET = 120.0
LOOKBACK = 7 * 86400.0
SHRINK = 25.0
MIN_N = 8
REFRESH = 3600.0
SCORE_EVERY = 3600.0
RETIRE_N = 30

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[int, tuple[int, float]]]] = {}
_seen: set[str] = set()
_scored_at = 0.0


def bucket_of(elapsed: float) -> int:
    return max(0, min(int(900.0 // BUCKET) - 1, int(elapsed // BUCKET)))


def record(store: Any, lane: str, now: float | None = None) -> dict[int, tuple[int, float]]:
    """{bucket: (settled bets, shrunk net per dollar staked)} for the lane over the last 7 days. Cached for an hour."""
    now = time.time() if now is None else now
    with _lock:
        got = _cache.get(lane)
        if got and now - got[0] < REFRESH:
            return got[1]
    rows = store.db.execute(
        "SELECT d.ts, d.side, d.outcome, d.contracts, d.stake_usd, w.close_ts FROM decisions d JOIN windows w ON w.ticker = d.ticker "
        "WHERE d.action='bet' AND d.mode='trade' AND COALESCE(d.lane,'moonshot')=? AND d.ts>? AND d.outcome IN ('yes','no') AND d.contracts>0 AND d.stake_usd>0 AND w.close_ts>0",
        (lane, now - LOOKBACK),
    ).fetchall()
    acc: dict[int, list[float]] = {}
    for r in rows:
        elapsed = float(r["ts"]) - (float(r["close_ts"]) - 900.0)
        if not 0.0 <= elapsed < 900.0:
            continue
        stake = float(r["stake_usd"])
        net = (float(r["contracts"]) - stake) if r["outcome"] == r["side"] else -stake
        a = acc.setdefault(bucket_of(elapsed), [0.0, 0.0, 0.0])
        a[0] += 1
        a[1] += net
        a[2] += stake
    out = {b: (int(a[0]), a[1] / (a[2] + SHRINK)) for b, a in acc.items()}
    with _lock:
        _cache[lane] = (now, out)
    return out


def verdict(store: Any, lane: str, elapsed: float, now: float | None = None) -> tuple[bool, int, int, float]:
    """(whether the lane sits this moment out, bucket, bets in it, its shrunk score). Open when the bucket has under MIN_N settled bets."""
    b = bucket_of(elapsed)
    n, score = record(store, lane, now).get(b, (0, 0.0))
    return (n >= MIN_N and score < 0.0), b, n, score


def _elapsed(shot: Any, now: float) -> float | None:
    if not str(shot.ticker).split("-")[0].endswith("15M"):
        return None
    try:
        return now - (datetime.fromisoformat(str(shot.close_time)).timestamp() - 900.0)
    except (TypeError, ValueError):
        return None


def allows(store: Any, shot: Any, spec: Any, now: float | None = None) -> tuple[bool, str]:
    """The live gate: (True, "") unless the lane has timing_gate and the record says sit this moment out."""
    now = time.time() if now is None else now
    if not getattr(spec, "timing_gate", False):
        return True, ""
    e = _elapsed(shot, now)
    if e is None:
        return True, ""
    skip, b, n, score = verdict(store, shot.lane, e, now)
    if skip:
        return False, f"timing: this lane's own record at {b * 2}-{b * 2 + 2} minutes into the window is {score:+.2f} per $ staked over {n} settled bets, so it sits this moment out."
    return True, ""


def shadow(s: Any, store: Any, shot: Any, spec: Any, ask: float, now: float | None = None) -> None:
    """Log, once per market side and bucket, a buy the lane's other rules cleared, with what the record says about this moment."""
    now = time.time() if now is None else now
    e = _elapsed(shot, now)
    if e is None or not 0.0 <= e < 900.0:
        return
    skip, b, n, score = verdict(store, shot.lane, e, now)
    key = f"{shot.ticker}|{shot.side}|{shot.lane}|{b}"
    path = Path(s.data_dir) / "timing_shadow.jsonl"
    with _lock:
        if not _seen:
            try:
                for line in path.read_text().splitlines():
                    r = json.loads(line)
                    if r.get("kind") == "signal" and now - r["ts"] < 86400.0:
                        _seen.add(f"{r['ticker']}|{r['side']}|{r['lane']}|{r['bucket']}")
            except (OSError, ValueError, KeyError):
                pass
        if key in _seen:
            return
        _seen.add(key)
    row = {"kind": "signal", "ts": round(now, 1), "ticker": shot.ticker, "lane": shot.lane, "side": shot.side, "ask": round(ask, 4), "elapsed": round(e, 1), "bucket": b, "n": n, "score": round(score, 4), "would_skip": bool(skip)}
    try:
        with path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        log.debug("timing shadow: could not write %s", path, exc_info=True)
    if skip:
        log.info("timing shadow: %s %s %s at %.2f, %.0f s into the window: the lane's record there is %+.2f per $ over %d bets, would sit this moment out", shot.lane, shot.ticker, shot.side, ask, e, score, n)


def run_score(s: Any, store: Any, now: float | None = None) -> list[dict[str, Any]]:
    """Hourly: score the logged buys whose window has settled, and log the 7-day totals for the buys the record would have kept and the ones it would have skipped."""
    global _scored_at
    from .strategy import fee_per_contract as fee

    now = time.time() if now is None else now
    if now - _scored_at < SCORE_EVERY:
        return []
    _scored_at = now
    path = Path(s.data_dir) / "timing_shadow.jsonl"
    try:
        rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        return []
    done = {(r["ticker"], r["side"], r["lane"], r["bucket"]) for r in rows if r.get("kind") == "score"}
    new: list[dict[str, Any]] = []
    for sig in rows:
        if sig.get("kind") != "signal" or (sig["ticker"], sig["side"], sig["lane"], sig["bucket"]) in done:
            continue
        got = store.db.execute("SELECT result FROM windows WHERE ticker=?", (sig["ticker"],)).fetchone()
        if not got or got["result"] not in ("yes", "no"):
            continue
        ask = sig["ask"]
        net = (((1.0 - ask) / ask) if got["result"] == sig["side"] else -1.0) - fee(ask) / ask
        new.append({"kind": "score", "ts": sig["ts"], "ticker": sig["ticker"], "side": sig["side"], "lane": sig["lane"], "bucket": sig["bucket"], "net": round(net, 4), "would_skip": sig["would_skip"]})
    if new:
        try:
            with path.open("a") as fh:
                for line in new:
                    fh.write(json.dumps(line) + "\n")
        except OSError:
            log.debug("timing shadow: could not write the scores", exc_info=True)
    recent = [r for r in rows + new if r.get("kind") == "score" and now - r["ts"] <= 7 * 86400.0]
    kept = [r for r in recent if not r["would_skip"]]
    skipped = [r for r in recent if r["would_skip"]]
    fmt = lambda xs: (len(xs), round(sum(r["net"] for r in xs) / len(xs), 3) if xs else 0.0)  # noqa: E731
    log.info("timing shadow (7 d, buys and mean net per $1): kept %s, would skip %s", fmt(kept), fmt(skipped))
    if len(skipped) >= RETIRE_N and sum(r["net"] for r in skipped) > 0:
        log.warning("timing shadow: the %d buys it would skip netted +%.3f per $1 in 7 days: the gate would have cost money", len(skipped), sum(r["net"] for r in skipped) / len(skipped))
    return new
