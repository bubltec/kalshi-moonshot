"""Varieties of a favorite, by how the spot has moved against the strike over the last five minutes, and a scoreboard of how each variety has paid.

A variety is read at the moment of the buy from three numbers, none of them tuned to the record:
  z       the cushion to the strike (in the bet's favor) in typical moves left: cushion / (spot's recent move per sqrt(second) x sqrt(seconds left))
  slope   the five-minute change of that cushion in typical five-minute moves (positive = the spot is moving away from the strike, in the bet's favor)
  crosses how many times the spot crossed the strike in those five minutes
Seven varieties come out (A to G, below). 2026-11-03, 2,298 market-wide favorites over nine days and the lanes' own bets: "far and flat" (B) paid in every view (91-96% won); "near but pulling away" (F)
paid NO and cost YES in both crypto and commodities; "near and flat" (E) cost crypto and paid commodities; oscillation (G) cost commodities. So what pays differs by asset class and side, and drifts, which is why the
scoreboard keeps each class and side apart and shows the latest days next to the rest. This module only reads: nothing here changes what a lane buys.
"""

from __future__ import annotations

import bisect
import itertools
import math
import statistics
import time
from collections import defaultdict
from collections.abc import Sequence

from . import db
from .biquotefeed import index_for_series as biquote_index
from .config import Settings
from .kalshiws import index_for_series as crypto_index
from .store import Store
from .strategy import fee_per_contract

FAR = 1.0  # a cushion of at least this many typical moves left is "far"
SLOPE = 0.5  # a five-minute change of this many typical five-minute moves is "moving"
CROSSES = 4  # this many strike crossings in five minutes is oscillation
LOOK = 300.0  # seconds of spot history a variety reads

NAMES = {
    "A": "far, still moving away",
    "B": "far, flat",
    "C": "far, coming back",
    "D": "near, approaching the strike",
    "E": "near, flat",
    "F": "near, pulling away",
    "G": "oscillating around the strike",
}


def classify(z: float, slope: float, crosses: int) -> str:
    """The variety letter for a cushion, a slope and a crossing count."""
    if crosses >= CROSSES:
        return "G"
    if z >= FAR:
        return "A" if slope >= SLOPE else "C" if slope <= -SLOPE else "B"
    return "D" if slope <= -SLOPE else "F" if slope >= SLOPE else "E"


def snapshot(ts: Sequence[float], values: Sequence[float], strike: float, side: str, at: float, close_ts: float) -> dict | None:
    """The three numbers and the letter at time `at`, from the spot's ticks (ascending times). None when the spot history is too thin or stale."""
    i = bisect.bisect_right(ts, at) - 1
    j = bisect.bisect_right(ts, at - LOOK) - 1
    if i < 0 or j < 0 or strike <= 0 or ts[i] < at - 10.0 or ts[j] < at - LOOK - 20.0 or close_ts - at <= 0:
        return None
    lo = bisect.bisect_left(ts, at - LOOK)
    if i - lo < 10:
        return None
    sq = 0.0
    for k in range(lo + 1, i + 1):
        sq += math.log(values[k] / values[k - 1]) ** 2
    dt = ts[i] - ts[lo]
    if dt <= 0 or sq <= 0:
        return None
    rate = math.sqrt(sq / dt)
    sign = 1.0 if side == "yes" else -1.0
    cushion = math.log(values[i] / strike) * sign
    before = math.log(values[j] / strike) * sign
    z = cushion / (rate * math.sqrt(close_ts - at))
    slope = (cushion - before) / (rate * math.sqrt(LOOK))
    seg = [x - strike for x in values[j : i + 1]]
    crosses = sum(1 for a, b in itertools.pairwise(seg) if (a > 0) != (b > 0))
    return {"z": z, "slope": slope, "crosses": crosses, "variety": classify(z, slope, crosses)}


def _pnl(row, held_fee: bool = True) -> float:
    contracts = row["stake_usd"] / row["ask"]
    if row["exit_price"] is not None:
        out = (row["exit_price"] - row["ask"]) * contracts - fee_per_contract(row["exit_price"]) * contracts
    elif row["outcome"] == row["side"]:
        out = (1.0 - row["ask"]) * contracts
    else:
        out = -row["stake_usd"]
    return out - (fee_per_contract(row["ask"]) * contracts if held_fee else 0.0)


def scoreboard(s: Settings, days: float = 12.0, recent_days: float = 3.0, now: float | None = None) -> dict[tuple[str, str, str], dict]:
    """Every settled or sold live bet of the favorite lanes, read as a variety at its buy and totalled by (asset class, side, variety). Each total carries all bets and the latest `recent_days` alone.
    Crypto reads the settlement index against the strike; commodities read BiQuote against its own price at the window open (the strike a Kalshi commodity market uses is a different feed's)."""
    now = time.time() if now is None else now
    tape = db.connect(s.tape_path, "tape", shared=True)
    store = Store(s.db_path, s.exclude_versions)
    rows = store.db.execute(
        "SELECT d.ts, d.lane, d.ticker, d.side, d.ask, d.stake_usd, d.exit_price, d.outcome, w.close_ts, w.strike, w.series FROM decisions d JOIN windows w ON w.ticker = d.ticker "
        "WHERE d.action = 'bet' AND d.mode = 'trade' AND (d.lane LIKE 'crypto_%' OR d.lane LIKE 'commodity_%' OR d.lane = 'commodity') AND d.lane NOT LIKE '%moonshot%' "
        "AND (d.outcome IN ('yes','no') OR d.exit_price IS NOT NULL) AND d.ts > ?",
        (now - days * 86400.0,),
    ).fetchall()
    cache: dict[str, tuple[list[float], list[float]]] = {}

    def ticks(index: str) -> tuple[list[float], list[float]]:
        if index not in cache:
            r = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (index, now - (days + 1) * 86400.0)).fetchall()
            cache[index] = ([float(x[0]) for x in r], [float(x[1]) for x in r])
        return cache[index]

    board: dict[tuple[str, str, str], dict] = defaultdict(lambda: {"n": 0, "wins": 0, "net": 0.0, "recent_n": 0, "recent_wins": 0, "recent_net": 0.0})
    for r in rows:
        series = r["series"]
        crypto = crypto_index(series)
        index = crypto or biquote_index(series)
        if not index:
            continue
        ts, values = ticks(index)
        if not ts:
            continue
        strike = float(r["strike"] or 0.0)
        if not crypto:  # a commodity's reference is the same feed's price when the window opened
            i0 = bisect.bisect_left(ts, r["close_ts"] - 900.0)
            if i0 >= len(ts) or ts[i0] - (r["close_ts"] - 900.0) > 20.0:
                continue
            strike = values[i0]
        snap = snapshot(ts, values, strike, r["side"], r["ts"], r["close_ts"])
        if snap is None:
            continue
        pnl = _pnl(r)
        cell = board[("crypto" if crypto else "commodity", r["side"], snap["variety"])]
        cell["n"] += 1
        cell["wins"] += pnl > 0
        cell["net"] += pnl
        if r["ts"] >= now - recent_days * 86400.0:
            cell["recent_n"] += 1
            cell["recent_wins"] += pnl > 0
            cell["recent_net"] += pnl
    return dict(board)


def report(s: Settings, days: float = 12.0, recent_days: float = 3.0, min_n: int = 5) -> list[str]:
    board = scoreboard(s, days, recent_days)
    out = [f"live favorite bets of the last {days:g} days by variety, fees in; 'recent' is the last {recent_days:g} days alone", "class       side  variety                              n  win%     net   $/bet | recent n  win%     net"]
    for (cls, side, v), c in sorted(board.items()):
        if c["n"] < min_n:
            continue
        rw = f"{c['recent_wins'] / c['recent_n'] * 100:4.0f}%" if c["recent_n"] else "   - "
        out.append(f"{cls:11} {side:4}  {v} {NAMES[v]:32} {c['n']:4d} {c['wins'] / c['n'] * 100:4.0f}% {c['net']:+8.1f} {c['net'] / c['n']:+6.2f} | {c['recent_n']:6d} {rw} {c['recent_net']:+8.1f}")
    pooled: dict[str, list[float]] = defaultdict(list)
    for (_cls, _side, v), c in board.items():
        pooled[v].append(c["net"])
    if pooled:
        out.append("")
        out.append("varieties pooled over class and side: " + ", ".join(f"{v} {sum(x):+.1f}" for v, x in sorted(pooled.items())))
    spread = statistics.pstdev([c["net"] / c["n"] for c in board.values() if c["n"] >= min_n]) if len(board) > 1 else 0.0
    out.append(f"spread of $/bet across the cells above: {spread:.2f}")
    return out
