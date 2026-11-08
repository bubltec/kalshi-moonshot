"""Shadow: is a shock on one coin a market-wide move or an isolated dip? (2026-11-08, user: "the crypto markets move together in those shock moments ... use it as a check that we don't need to sell, or if it stays for 3 s on the check we should sell").
studies/results/2026-11-08-shock-breadth.txt: at the 131 live crypto shock-exit sales, 2.24 of the nine coins were moving a typical minute against the bet in 60 s (1.08 at random seconds). Sold with 0-1 coins moving (76 sales, isolated) the sale lost to holding, -0.123 +/- 0.183 per sale; with 4-6 coins moving it beat holding, +0.891 +/- 0.195 (16 of 16). studies/results/2026-11-08-shock-breadth-rule.txt: 'sell at once when K = 2 or more coins confirm, otherwise wait until the shock conditions have held P = 3 s' netted +$9.25 against the current exit on 858 bets (halves +11.27 / -2.02): not a rule yet.
This module only logs. Each time a lane's shock exit fires on a crypto 15-minute bet, it counts how many of the crypto indexes (the family, not a coin rule) moved at least one typical minute against the bet's direction over the last 60 s, from the in-memory ticks, and writes what the check would do: sell now (B >= K) or wait P seconds. It places no order and the live shock exit is unchanged.
Hourly, the bets whose window has settled are scored: the sale the live exit made against holding to the result, split into the group the check would have deferred (B < K) and the one it would sell at once. A deferred group whose sales beat holding over RETIRE_N events in 7 days is flagged (CLAUDE.md rule 6): the check would have cost money. The 3 s persistence is replayed offline (scripts/shock_breadth_rule_replay.py) from the tape; the live log carries the breadth.
Lines go to DATA_DIR/breadth_shadow.jsonl: kind "fire" and kind "score"."""

from __future__ import annotations

import bisect
import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("moonshot")

K = 2
P = 3
SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M", "KXNEAR15M", "KXBNB15M", "KXHYPE15M", "KXZEC15M")  # the crypto 15-minute family whose indexes are read
SPAN = 60.0
SCORE_EVERY = 3600.0
RETIRE_N = 30

_lock = threading.Lock()
_seen: set[str] = set()
_scored_at = 0.0


def breadth_against(sign: float, now: float | None = None) -> tuple[int, int]:
    """(how many of the crypto indexes moved a typical minute or more against a bet of direction `sign` (1 = YES) over the last 60 s, how many could be read), from the in-memory ticks. Each index in its own typical minute (variants.unit_index)."""
    from . import ticks as ticks_mod
    from . import variants

    now = time.time() if now is None else now
    count = seen = 0
    for ser in SERIES:
        ix = variants.crypto_index(ser)
        if not ix:
            continue
        unit = variants.unit_index(ix, now)
        got = ticks_mod.history.window(ix, now - SPAN - 15.0, now)
        if unit is None or unit <= 0 or not got or not got[0]:
            continue
        ts, vs = got
        i, j = bisect.bisect_right(ts, now) - 1, bisect.bisect_right(ts, now - SPAN) - 1
        if i < 0 or j < 0 or vs[i] <= 0 or vs[j] <= 0 or ts[i] < now - 10.0:
            continue
        seen += 1
        if -math.log(vs[i] / vs[j]) * sign / unit >= 1.0:
            count += 1
    return count, seen


def shadow_fire(s: Any, ticker: str, lane: str, side: str, now: float, bid: float, left: float, cushion_bp: float, worst_bp: float) -> None:
    """Log, once per bet, the breadth at the moment the shock exit fired and what the check would do. Never raises."""
    try:
        key = f"{ticker}|{lane}"
        path = Path(s.data_dir) / "breadth_shadow.jsonl"
        with _lock:
            if not _seen:
                try:
                    for line in path.read_text().splitlines():
                        r = json.loads(line)
                        if r.get("kind") == "fire" and now - r["ts"] < 86400.0:
                            _seen.add(f"{r['ticker']}|{r['lane']}")
                except (OSError, ValueError, KeyError):
                    pass
            if key in _seen:
                return
            _seen.add(key)
        b, seen = breadth_against(1.0 if side == "yes" else -1.0, now)
        would = "sell now" if b >= K else f"wait {P}s"
        row = {"kind": "fire", "ts": round(now, 1), "ticker": ticker, "lane": lane, "side": side, "left": round(left, 1), "bid": round(bid, 3), "breadth": b, "seen": seen, "k": K, "p": P, "would": would, "cushion_bp": round(cushion_bp, 2), "worst_bp": round(worst_bp, 2)}
        with path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        log.info("breadth shadow: %s %s %s, shock exit fired with %d of %d coins moving against it: would %s", lane, ticker, side, b, seen, would)
    except Exception:
        log.debug("breadth shadow failed", exc_info=True)


def run_score(s: Any, store: Any, now: float | None = None) -> list[dict[str, Any]]:
    """Hourly: score the fired bets whose window has settled and log the 7-day totals for the group the check would defer (B < K) and the group it would sell at once."""
    global _scored_at
    from .strategy import fee_per_contract as fee

    now = time.time() if now is None else now
    if now - _scored_at < SCORE_EVERY:
        return []
    _scored_at = now
    path = Path(s.data_dir) / "breadth_shadow.jsonl"
    try:
        rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        return []
    done = {(r["ticker"], r["lane"]) for r in rows if r.get("kind") == "score"}
    new: list[dict[str, Any]] = []
    for f in rows:
        if f.get("kind") != "fire" or (f["ticker"], f["lane"]) in done:
            continue
        got = store.db.execute(
            "SELECT d.contracts, d.exit_price, d.side, w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
            "WHERE d.ticker=? AND COALESCE(d.lane,'moonshot')=? AND d.action='bet' AND d.exit_reason LIKE 'shock exit%' AND d.exit_price>0 AND d.contracts>0 ORDER BY d.exit_ts DESC LIMIT 1",
            (f["ticker"], f["lane"]),
        ).fetchone()
        if not got or got["result"] not in ("yes", "no"):
            continue
        n, bid = float(got["contracts"]), float(got["exit_price"])
        hold = n if got["result"] == (got["side"] or "yes") else 0.0
        new.append({"kind": "score", "ts": f["ts"], "ticker": f["ticker"], "lane": f["lane"], "breadth": f["breadth"], "change": round(n * (bid - fee(bid)) - hold, 4), "defer": f["breadth"] < f["k"]})
    if new:
        try:
            with path.open("a") as fh:
                for line in new:
                    fh.write(json.dumps(line) + "\n")
        except OSError:
            log.debug("breadth shadow: could not write the scores", exc_info=True)
    recent = [r for r in rows + new if r.get("kind") == "score" and now - r["ts"] <= 7 * 86400.0]
    defer = [r for r in recent if r["defer"]]
    now_group = [r for r in recent if not r["defer"]]
    fmt = lambda xs: (len(xs), round(sum(r["change"] for r in xs) / len(xs), 3) if xs else 0.0)  # noqa: E731
    log.info("breadth shadow (7 d, shock sales and mean change against holding): the check would defer %s, would sell at once %s", fmt(defer), fmt(now_group))
    if len(defer) >= RETIRE_N and sum(r["change"] for r in defer) > 0:
        log.warning("breadth shadow: the %d sales the check would defer beat holding by %.3f per sale in 7 days: the check would have cost money", len(defer), sum(r["change"] for r in defer) / len(defer))
    return new
