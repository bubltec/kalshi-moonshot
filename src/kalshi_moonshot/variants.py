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


GRID = 1.0  # seconds: both feeds are read on this grid, so a crossing means the same on a 2.5 s crypto index and a 1 s commodity feed


def snapshot(ts: Sequence[float], values: Sequence[float], strike: float, side: str, at: float, close_ts: float, grid: float = GRID) -> dict | None:
    """The three numbers and the letter at time `at`, from the spot's ticks (ascending times). None when the spot history is too thin or stale.
    With `grid` > 0 the last LOOK seconds are first put on an even grid (each point is the latest tick at or before it), so the volatility and the crossings do not depend on how often a feed ticks; 0 reads the raw ticks."""
    i = bisect.bisect_right(ts, at) - 1
    j = bisect.bisect_right(ts, at - LOOK) - 1
    if i < 0 or j < 0 or strike <= 0 or ts[i] < at - 10.0 or ts[j] < at - LOOK - 20.0 or close_ts - at <= 0:
        return None
    lo = bisect.bisect_left(ts, at - LOOK)
    if i - lo < 10:
        return None
    if grid > 0:
        steps = int(LOOK / grid)
        points = []
        for k in range(steps + 1):
            idx = bisect.bisect_right(ts, at - LOOK + k * grid) - 1
            if idx >= 0:
                points.append(values[idx])
        seq = points
        dt = (len(seq) - 1) * grid
    else:
        seq = list(values[lo : i + 1])
        dt = ts[i] - ts[lo]
    if len(seq) < 10 or dt <= 0:
        return None
    sq = sum(math.log(b / a) ** 2 for a, b in itertools.pairwise(seq))
    if sq <= 0:
        return None
    rate = math.sqrt(sq / dt)
    sign = 1.0 if side == "yes" else -1.0
    cushion = math.log(values[i] / strike) * sign
    before = math.log(values[j] / strike) * sign
    z = cushion / (rate * math.sqrt(close_ts - at))
    slope = (cushion - before) / (rate * math.sqrt(LOOK))
    crosses = sum(1 for a, b in itertools.pairwise([x - strike for x in seq]) if (a > 0) != (b > 0))
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


COINS = {"KXBTC15M": "BTC", "KXETH15M": "ETH", "KXSOL15M": "SOL", "KXXRP15M": "XRP", "KXDOGE15M": "DOGE"}


def favorite_entry(quotes: Sequence[tuple[float, float, float]], close_ts: float, band: tuple[float, float] = (0.78, 0.92), max_spread: float = 0.03, left: tuple[float, float] = (480.0, 120.0)) -> tuple[str, float, float, float] | None:
    """The first moment a crypto favorite lane would buy a window: from `left[0]` to `left[1]` seconds before the close, a side whose ask is inside `band` with a spread of `max_spread` or less.
    `quotes` are (time, yes bid, yes ask) ascending. Returns (side, ask, time, bid) with the side's own bid at that moment, or None when no side qualified."""
    for at, yes_bid, yes_ask in quotes:
        remaining = close_ts - at
        if remaining > left[0] or remaining < left[1]:
            continue
        for side, bid, ask in (("yes", yes_bid, yes_ask), ("no", 1.0 - yes_ask, 1.0 - yes_bid)):
            if band[0] <= ask <= band[1] and ask - bid <= max_spread + 1e-9:
                return side, ask, at, bid
    return None


def market_scoreboard(s: Settings, days: float = 6.0, recent_days: float = 1.0, now: float | None = None) -> dict[tuple[str, str, str, str], dict]:
    """Every crypto and commodity window's favorite (favorite_entry), read as a variety at the moment of its buy and held to the result, whether or not a lane bought it. This keeps the observation going while a lane
    is paused, for instance by the choppy rule. Totals by (class, market, side, variety) carry the count, the wins (a loss settles at zero) and the net of a 10-dollar ticket with the taker fee, all and `recent_days` alone,
    and the flip: buying the OTHER side at its ask (one minus the favorite's bid) instead, which wins when the favorite loses. Commodities are read against BiQuote's price at the window open, so they only go back to when that feed began."""
    now = time.time() if now is None else now
    tape = db.connect(s.tape_path, "tape", shared=True)
    store = Store(s.db_path, s.exclude_versions)
    windows = store.db.execute(
        "SELECT ticker, series, close_ts, strike, result FROM windows WHERE close_ts BETWEEN ? AND ? AND result IN ('yes','no') AND strike > 0 ORDER BY close_ts",
        (now - days * 86400.0, now - 600.0),
    ).fetchall()
    ticks: dict[str, tuple[list[float], list[float]]] = {}

    def feed(index: str) -> tuple[list[float], list[float]]:
        if index not in ticks:
            r = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (index, now - (days + 1) * 86400.0)).fetchall()
            ticks[index] = ([float(x[0]) for x in r], [float(x[1]) for x in r])
        return ticks[index]

    keys = ("n", "wins", "net", "recent_n", "recent_wins", "recent_net", "flip_wins", "flip_net", "flip_price", "recent_flip_net")
    board: dict[tuple[str, str, str, str], dict] = defaultdict(lambda: dict.fromkeys(keys, 0.0))
    for w in windows:
        series = w["series"]
        crypto = crypto_index(series)
        index = crypto or biquote_index(series)
        if not index:
            continue
        close = float(w["close_ts"])
        q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], close - 480.0, close - 120.0)).fetchall()
        pick = favorite_entry([(float(x[0]), float(x[1]), float(x[2])) for x in q], close)
        if pick is None:
            continue
        side, ask, at, bid = pick
        ts, values = feed(index)
        strike = float(w["strike"])
        if not crypto:
            i0 = bisect.bisect_left(ts, close - 900.0)
            if i0 >= len(ts) or ts[i0] - (close - 900.0) > 20.0:
                continue
            strike = values[i0]
        snap = snapshot(ts, values, strike, side, at, close)
        if snap is None:
            continue
        contracts = 10.0 / ask
        won = w["result"] == side
        pnl = ((1.0 - ask) * contracts if won else -10.0) - fee_per_contract(ask) * contracts
        other = max(1.0 - bid, 0.02)  # the other side's ask
        flip_contracts = 10.0 / other
        flip = ((1.0 - other) * flip_contracts if not won else -10.0) - fee_per_contract(other) * flip_contracts
        name = COINS.get(series) or series[2:-3]
        cell = board[("crypto" if crypto else "commodity", name, side, snap["variety"])]
        recent = at >= now - recent_days * 86400.0
        cell["n"] += 1
        cell["wins"] += won
        cell["net"] += pnl
        cell["flip_wins"] += not won
        cell["flip_net"] += flip
        cell["flip_price"] += other
        if recent:
            cell["recent_n"] += 1
            cell["recent_wins"] += won
            cell["recent_net"] += pnl
            cell["recent_flip_net"] += flip
    return dict(board)


def market_report(s: Settings, days: float = 6.0, recent_days: float = 1.0, min_n: int = 8) -> list[str]:
    """The market-wide scoreboard by class, side and variety with every market pooled, with what flipping to the other side would have made, then the zero rate of each crypto variety by coin."""
    board = market_scoreboard(s, days, recent_days)
    pooled: dict[tuple[str, str, str], dict] = defaultdict(lambda: defaultdict(float))
    by_coin: dict[tuple[str, str], dict] = defaultdict(lambda: {"n": 0, "wins": 0})
    for (cls, name, side, v), cell in board.items():
        p = pooled[(cls, side, v)]
        for k, x in cell.items():
            p[k] += x
        if cls == "crypto":
            bc = by_coin[(v, name)]
            bc["n"] += cell["n"]
            bc["wins"] += cell["wins"]
    out = [
        f"every favorite of the last {days:g} days by variety (a lane or not), held to the result on a $10 ticket with the taker fee; a loss settles at zero. 'recent' is the last {recent_days:g} day(s).",
        "flip = buying the other side at its ask instead; it wins when the favorite loses (its price is about 0.1-0.25, so it wins about one time in five).",
    ]
    for cls in ("crypto", "commodity"):
        out.append("")
        out.append(f"{cls}:  side  variety                              n  zeros%     net   $/bet | flip wins%  price  $/bet | recent n  zeros%     net  flip $")
        for (klass, side, v), x in sorted(pooled.items(), key=lambda kv: (kv[0][2], kv[0][1])):
            if klass != cls or x["n"] < min_n:
                continue
            rz = f"{(1 - x['recent_wins'] / x['recent_n']) * 100:5.0f}%" if x["recent_n"] else "    - "
            out.append(
                f"{'':{len(cls) + 1}} {side:4}  {v} {NAMES[v]:32} {int(x['n']):4d} {(1 - x['wins'] / x['n']) * 100:5.0f}% {x['net']:+8.1f} {x['net'] / x['n']:+6.2f} | "
                f"{x['flip_wins'] / x['n'] * 100:8.0f}% {x['flip_price'] / x['n']:6.2f} {x['flip_net'] / x['n']:+6.2f} | {int(x['recent_n']):6d} {rz} {x['recent_net']:+8.1f} {x['recent_flip_net']:+7.1f}"
            )
    out.append("")
    out.append("crypto zero rate (share of favorites that settled at zero) of each variety by coin, with the count:")
    coins = list(COINS.values())
    out.append("variety                              " + " ".join(f"{c:>9}" for c in coins))
    for v in sorted(NAMES):
        cells = []
        for coin in coins:
            coin_cell = by_coin.get((v, coin))
            cells.append(
                f"{(1 - coin_cell['wins'] / coin_cell['n']) * 100:4.0f}%/{int(coin_cell['n']):3d}"
                if coin_cell and coin_cell["n"] >= 5
                else "        -"
            )
        out.append(f"{v} {NAMES[v]:34} " + " ".join(f"{x:>9}" for x in cells))
    return out


def variety_now(s: Settings, series: str, side: str, left_seconds: float, now: float | None = None, strike: float | None = None) -> str | None:
    """The variety a bet would be at this moment, from the index ticks of the last five minutes. A commodity reads BiQuote against its own price when the window opened; a crypto coin reads its settlement index against
    the window's `strike`. None when the series has no index, the reference is missing (no BiQuote tick near the open, or no strike for a coin), or there are too few recent ticks:
    a caller that skips on a variety must not skip on None."""
    crypto = crypto_index(series)
    index = crypto or biquote_index(series)
    if not index or left_seconds <= 0:
        return None
    now = time.time() if now is None else now
    close = now + left_seconds
    tape = db.connect(s.tape_path, "tape", shared=True)
    if crypto:
        if not strike or strike <= 0:
            return None
        ref = strike
    else:
        opened = close - 900.0
        row = tape.execute("SELECT value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts LIMIT 1", (index, opened - 5.0, opened + 20.0)).fetchone()
        if not row:
            return None
        ref = float(row[0])
    rows = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (index, now - LOOK - 40.0, now)).fetchall()
    snap = snapshot([float(r[0]) for r in rows], [float(r[1]) for r in rows], ref, side, now, close)
    return snap["variety"] if snap else None


def skipped(varieties: Sequence[str], variety: str | None, side: str) -> bool:
    """Whether a lane's skip list names this variety: "G" skips it on either side, "A-yes" only on that side."""
    return variety is not None and (variety in varieties or f"{variety}-{side}" in varieties)


TUNED_FILE = "tuned_varieties.json"
TUNE_MAX_AGE = 3 * 86400.0  # a tuned list older than this is ignored: the markets it described have moved on
_tuned_cache: dict[str, tuple[float, dict]] = {}


def tune(s: Settings, days: float = 10.0, min_n: int = 40, min_loss: float = 0.10, now: float | None = None) -> dict:
    """The cells of the market-wide scoreboard that lost in BOTH halves of the last `days` days, so a lane can stop buying them without anyone reading a report.

    The window is cut in two (older, newer) and each half is scored on its own with market_scoreboard. A (class, side, variety) cell is skipped only when each half has at least min_n/2 favorites and lost
    at least `min_loss` dollars a $10 bet in each: a cell that lost in one half and won in the other is noise, and a cell that merely lost a cent is not worth a rule."""
    now = time.time() if now is None else now
    half = days / 2.0
    older = market_scoreboard(s, half, half, now - half * 86400.0)
    newer = market_scoreboard(s, half, half, now)

    def pooled(board: dict) -> dict[tuple[str, str, str], tuple[float, float]]:
        out: dict[tuple[str, str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
        for (cls, _name, side, v), c in board.items():
            out[(cls, side, v)][0] += c["n"]
            out[(cls, side, v)][1] += c["net"]
        return {k: (n, net) for k, (n, net) in out.items()}

    a, b = pooled(older), pooled(newer)
    skip: dict[str, list[str]] = {"crypto": [], "commodity": []}
    cells: dict[str, dict[str, float]] = {}
    for key in sorted(set(a) & set(b)):
        (n1, net1), (n2, net2) = a[key], b[key]
        cls, side, v = key
        cells[f"{cls}:{v}-{side}"] = {"n_old": n1, "per_bet_old": round(net1 / n1, 3), "n_new": n2, "per_bet_new": round(net2 / n2, 3)}
        if n1 >= min_n / 2 and n2 >= min_n / 2 and net1 / n1 <= -min_loss and net2 / n2 <= -min_loss:
            skip[cls].append(f"{v}-{side}")
    return {"at": now, "days": days, "min_n": min_n, "min_loss": min_loss, "skip": skip, "cells": cells}


def write_tuned(s: Settings, **kw) -> dict:
    """Run tune() and save it where the lanes read it (data_dir/tuned_varieties.json), replacing the old file in one step."""
    import json
    import os

    out = tune(s, **kw)
    path = s.data_dir / TUNED_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1))
    os.replace(tmp, path)
    _tuned_cache.pop(str(path), None)
    return out


def tuned_skips(s: Settings, series: str, now: float | None = None) -> tuple[str, ...]:
    """The tuned skip list for this series asset class: () when there is no file, it is older than TUNE_MAX_AGE, or it cannot be read."""
    import json

    cls = "crypto" if crypto_index(series) else "commodity" if biquote_index(series) else None
    if cls is None:
        return ()
    path = s.data_dir / TUNED_FILE
    try:
        mtime = path.stat().st_mtime
        hit = _tuned_cache.get(str(path))
        if hit is None or hit[0] != mtime:
            hit = (mtime, json.loads(path.read_text()))
            _tuned_cache[str(path)] = hit
    except (OSError, ValueError):
        return ()
    data = hit[1]
    now = time.time() if now is None else now
    if now - float(data.get("at", 0.0)) > TUNE_MAX_AGE:
        return ()
    return tuple(data.get("skip", {}).get(cls, ()))
