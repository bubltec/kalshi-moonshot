"""Shadow: sit out when the whole crypto market is running hot (2026-11-08, user: "ok go for it" after studies/results/2026-11-08-regime-sitout.txt).
323 live crypto_multiscale bets since 11-05, by how many of the nine crypto indexes were running at 1.5x or more of their own normal volatility at the buy (the last 15 minutes of one-second root-mean-square move over the median of that index's 15-minute readings of the previous 24 hours; rule 1: in the market's own units, no coin names): 0-1 hot -0.128 +/- 0.076 per $ staked, 2-4 hot +0.289 +/- 0.152, 5 or more hot -0.270 +/- 0.061 (63 bets, -$34.10 of the lane's -$39.85, both halves negative). Two days of bets, so it starts in shadow (rule 5).
Each buy the lane's other rules clear is logged once per market side with the hot count and whether the check would skip it (HOT_MIN or more indexes at RATIO_MIN or more), to DATA_DIR/hot_shadow.jsonl, and scored hourly against the settled window (net per $1 at the ask after the full taker fee, held to the result), kept against would-skip. A would-skip group that is positive over RETIRE_N signals in 7 days is flagged (rule 6): the gate would have cost money. A lane skips for real only with hot_gate = true (default off, the live switch is the user's).
The baseline (each index's median 15-minute reading over the previous 24 hours) is rebuilt hourly from the tape in the background thread (rule 2: a cutoff comes from the market's own recent history); the live reading is variants.unit_index over the last 15 minutes of in-memory ticks."""

from __future__ import annotations

import json
import logging
import math
import statistics
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("moonshot")

RATIO_MIN = 1.5
HOT_MIN = 5
FAMILIES = {
    "crypto": ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXNEAR15M", "KXBNB15M", "KXHYPE15M", "KXZEC15M"),
    "commodity": ("KXGOLD15M", "KXSILVER15M", "KXPLATINUM15M", "KXPALLADIUM15M", "KXWTI15M", "KXBRENT15M", "KXNATGAS15M", "KXCOPPER15M"),
}  # 2026-11-09 (user: a commodity lane should only use hot commodity markets): each family's own 15-minute indexes are read, and a lane is judged by the family its series settle in (the index kind), never by a name. Not a per-coin or per-metal rule
SERIES = FAMILIES["crypto"]  # kept for the breadth shadow's old import
REFRESH = 3600.0
SCORE_EVERY = 3600.0
RETIRE_N = 30
WINDOWS = 96  # 15-minute readings: 24 hours

_lock = threading.Lock()
_baseline: dict[str, float] = {}  # index -> median 15 minute root-mean-square one-second log move
_baseline_at = 0.0
_seen: set[str] = set()
_scored_at = 0.0


def family_of(series: Any) -> str | None:
    """The family ("crypto" or "commodity") a series or a lane's series list belongs to, by the kind of index it settles on; None when it has no index of either kind."""
    from . import variants

    first = (series[0] if series else None) if isinstance(series, (list, tuple)) else series
    if not first:
        return None
    return "crypto" if variants.crypto_index(first) else "commodity" if variants.biquote_index(first) else None


def hot_min(family: str) -> int:
    """How many of a family's indexes must run hot (and be readable) for the family to count as hot: a majority, which is 5 of the 9 crypto indexes as before and 4 of the 8 commodity ones."""
    return (len(FAMILIES[family]) + 1) // 2


def refresh(tape: Any, now: float | None = None) -> None:
    """Rebuild each index's baseline from the tape (at most hourly). Never raises."""
    global _baseline_at
    from . import variants

    now = time.time() if now is None else now
    if tape is None or now - _baseline_at < REFRESH:
        return
    _baseline_at = now
    out: dict[str, float] = {}
    try:
        for ser in [x for fam in FAMILIES.values() for x in fam]:
            ix = variants.crypto_index(ser) or variants.biquote_index(ser)
            if not ix:
                continue
            r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (ix, now - 86400.0 - 1800.0, now)).fetchall()
            if len(r) < 5000:
                continue
            t0 = int(now - 86400.0 - 1800.0)
            ts = [float(x[0]) for x in r]
            vs = [math.log(float(x[1])) for x in r]
            j, last, prev, sq = 0, None, None, [0.0]
            for t in range(t0, int(now) + 1):
                while j < len(ts) and ts[j] <= t:
                    last = vs[j]
                    j += 1
                sq.append(sq[-1] + ((last - prev) ** 2 if last is not None and prev is not None else 0.0))
                prev = last
            end = len(sq) - 1
            reads = [math.sqrt(max(sq[end - 900 * k] - sq[end - 900 * (k + 1)], 0.0) / 900.0) for k in range(1, WINDOWS + 1) if end - 900 * (k + 1) >= 0]
            reads = [x for x in reads if x > 0]
            if len(reads) >= 48:
                out[ix] = statistics.median(reads)
    except Exception:
        log.debug("hot check: the baseline refresh failed", exc_info=True)
        return
    with _lock:
        _baseline.update(out)


def hot_count(now: float | None = None, family: str = "crypto") -> tuple[int, int, dict[str, float]]:
    """(how many of the family's indexes run at RATIO_MIN or more of their own normal, how many could be read, each index's ratio) from the in-memory ticks and the hourly baseline."""
    from . import variants

    now = time.time() if now is None else now
    hot = seen = 0
    ratios: dict[str, float] = {}
    for ser in FAMILIES[family]:
        ix = variants.crypto_index(ser) or variants.biquote_index(ser)
        base = _baseline.get(ix or "")
        unit = variants.unit_index(ix, now) if ix else None
        if not ix or not base or unit is None or unit <= 0:
            continue
        ratio = unit / (base * math.sqrt(60.0))
        ratios[ser] = ratio
        seen += 1
        if ratio >= RATIO_MIN:
            hot += 1
    return hot, seen, ratios


def allows(spec: Any, now: float | None = None) -> tuple[bool, str]:
    """The live gate: (True, "") unless the lane has hot_gate and the market is hot."""
    if not getattr(spec, "hot_gate", False):
        return True, ""
    series = getattr(spec, "series", ())
    family = family_of(series) if series else "crypto"  # a spec that names no series is the original crypto check
    if family is None:
        return True, ""
    hot, seen, _ = hot_count(now, family)
    need = hot_min(family)
    if seen >= need and hot >= need:
        return False, f"hot market: {hot} of {seen} {family} indexes are running at {RATIO_MIN:g}x or more of their own normal volatility, so this lane sits out."
    return True, ""


def shadow(s: Any, shot: Any, spec: Any, ask: float, now: float | None = None) -> None:
    """Log, once per market side, a buy the lane's other rules cleared, with the hot count and what the check would do. Never raises."""
    try:
        now = time.time() if now is None else now
        family = family_of(str(shot.series))
        if family is None:
            return
        hot, seen, ratios = hot_count(now, family)
        need = hot_min(family)
        if seen < need:
            return
        key = f"{shot.ticker}|{shot.side}|{shot.lane}"
        path = Path(s.data_dir) / "hot_shadow.jsonl"
        with _lock:
            if not _seen:
                try:
                    for line in path.read_text().splitlines():
                        r = json.loads(line)
                        if r.get("kind") == "signal" and now - r["ts"] < 86400.0:
                            _seen.add(f"{r['ticker']}|{r['side']}|{r['lane']}")
                except (OSError, ValueError, KeyError):
                    pass
            if key in _seen:
                return
            _seen.add(key)
        own = ratios.get(str(shot.series), None)
        skip = hot >= need
        row = {"kind": "signal", "ts": round(now, 1), "ticker": shot.ticker, "lane": shot.lane, "side": shot.side, "ask": round(ask, 4), "hot": hot, "seen": seen, "own": round(own, 3) if own is not None else None, "would_skip": bool(skip)}
        with path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        if skip:
            log.info("hot shadow: %s %s %s at %.2f: %d of %d crypto indexes at %gx or more of their normal volatility, would sit out", shot.lane, shot.ticker, shot.side, ask, hot, seen, RATIO_MIN)
    except Exception:
        log.debug("hot shadow failed", exc_info=True)


def run_score(s: Any, store: Any, now: float | None = None) -> list[dict[str, Any]]:
    """Hourly: score the logged buys whose window has settled and log the 7-day totals for the buys the check would keep and the ones it would skip."""
    global _scored_at
    from .strategy import fee_per_contract as fee

    now = time.time() if now is None else now
    if now - _scored_at < SCORE_EVERY:
        return []
    _scored_at = now
    path = Path(s.data_dir) / "hot_shadow.jsonl"
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
        new.append({"kind": "score", "ts": sig["ts"], "ticker": sig["ticker"], "side": sig["side"], "lane": sig["lane"], "net": round(net, 4), "would_skip": sig["would_skip"]})
    if new:
        try:
            with path.open("a") as fh:
                for line in new:
                    fh.write(json.dumps(line) + "\n")
        except OSError:
            log.debug("hot shadow: could not write the scores", exc_info=True)
    recent = [r for r in rows + new if r.get("kind") == "score" and now - r["ts"] <= 7 * 86400.0]
    kept = [r for r in recent if not r["would_skip"]]
    skipped = [r for r in recent if r["would_skip"]]
    fmt = lambda xs: (len(xs), round(sum(r["net"] for r in xs) / len(xs), 3) if xs else 0.0)  # noqa: E731
    log.info("hot shadow (7 d, buys and mean net per $1): kept %s, would skip %s", fmt(kept), fmt(skipped))
    if len(skipped) >= RETIRE_N and sum(r["net"] for r in skipped) > 0:
        log.warning("hot shadow: the %d buys it would skip netted +%.3f per $1 in 7 days: the gate would have cost money", len(skipped), sum(r["net"] for r in skipped) / len(skipped))
    return new
