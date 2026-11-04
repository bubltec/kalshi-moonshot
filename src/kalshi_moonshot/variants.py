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

import ast
import bisect
import itertools
import logging
import math
import statistics
import threading
import time
from collections import defaultdict, deque
from collections.abc import Iterator, Sequence
from typing import Any

from . import db
from . import ticks as ticks_mod
from .biquotefeed import index_for_series as biquote_index
from .config import Settings
from .kalshiws import index_for_series as crypto_index
from .store import Store
from .strategy import fee_per_contract

log = logging.getLogger("kalshi_moonshot.variants")

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


CUSTOM_FILE = "varieties.toml"
FEATURES = ("z", "slope", "crosses", "vol_ratio", "since_cross")  # the numbers a custom variety may test: see snapshot()
_BASE_NAMES = dict(NAMES)
_custom: list[tuple[str, str, Any]] = []  # (letter, name, compiled test), tried in file order before the built-in letters
_custom_mtime: dict[str, float] = {}

_ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Not, ast.UnaryOp, ast.USub, ast.UAdd, ast.Compare, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq,
            ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Name, ast.Load, ast.Constant)


def compile_test(expr: str) -> Any:
    """A custom variety's `when` text as a checked expression over FEATURES only: comparisons, and/or/not and plain arithmetic. Anything else (a call, an attribute, a name that is not a feature) is refused."""
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ValueError(f"not allowed in a variety test: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in FEATURES:
            raise ValueError(f"unknown feature {node.id!r}; the features are {', '.join(FEATURES)}")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            raise ValueError("only numbers may appear in a variety test")
    return compile(tree, "<variety>", "eval")


def load_custom(s: Settings | Any) -> None:
    """Read data_dir/varieties.toml when it changed: [[variety]] entries with a letter (not one of A-G), a name and a `when` test. A bad entry is logged and left out; the built-in letters always work."""
    import tomllib

    path = s.data_dir / CUSTOM_FILE
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = -1.0
    if _custom_mtime.get(str(path)) == mtime:
        return
    _custom_mtime[str(path)] = mtime
    found: list[tuple[str, str, Any]] = []
    if mtime >= 0:
        try:
            entries = tomllib.loads(path.read_text()).get("variety", [])
        except (OSError, ValueError):
            log.warning("variants: %s could not be read; custom varieties are off", path, exc_info=True)
            entries = []
        for e in entries:
            letter = str(e.get("letter", "")).strip()
            try:
                if not letter or letter in _BASE_NAMES:
                    raise ValueError("a custom variety needs its own letter, not A-G")
                found.append((letter, str(e.get("name", letter)), compile_test(str(e["when"]))))
            except (KeyError, ValueError, SyntaxError) as exc:
                log.warning("variants: custom variety %r skipped: %s", e.get("letter"), exc)
    _custom[:] = found
    NAMES.clear()
    NAMES.update(_BASE_NAMES)
    NAMES.update({letter: name for letter, name, _ in found})


def classify_features(f: dict[str, float]) -> str:
    """The variety of a set of features: the first custom variety whose test holds, else the built-in letter."""
    for letter, _name, test in _custom:
        try:
            if eval(test, {"__builtins__": {}}, f):  # the test was checked by compile_test
                return letter
        except (ZeroDivisionError, TypeError):
            continue
    return classify(f["z"], f["slope"], int(f["crosses"]))


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
    sides = [(x - strike) > 0 for x in seq]
    crosses = sum(1 for a, b in itertools.pairwise(sides) if a != b)
    last = next((k for k in range(len(sides) - 1, 0, -1) if sides[k] != sides[k - 1]), None)
    since_cross = LOOK if last is None else (len(sides) - 1 - last) * (dt / (len(sides) - 1))
    tail = seq[-61:] if grid > 0 else seq[-max(2, len(seq) // 5):]  # the last minute on the grid (a fifth of the ticks off it)
    tail_sq = sum(math.log(b / a) ** 2 for a, b in itertools.pairwise(tail))
    tail_dt = (len(tail) - 1) * grid if grid > 0 else dt * (len(tail) - 1) / max(1, len(seq) - 1)
    vol_ratio = math.sqrt(tail_sq / tail_dt) / rate if tail_dt > 0 else 1.0
    f = {"z": z, "slope": slope, "crosses": crosses, "vol_ratio": vol_ratio, "since_cross": since_cross}
    return {**f, "variety": classify_features(f)}


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


def favorites(s: Settings, start: float, end: float) -> Iterator[dict]:
    """Every crypto and commodity window that closed in (start, end] and has a result, as its favorite (favorite_entry) read as a variety at the moment of the buy: the features, the side, the price, whether it won, what a $10 ticket
    made with the taker fee, and what the flip (the other side) would have made. One dict per window, whether or not a lane bought it. Commodities are read against BiQuote's price at the window open."""
    load_custom(s)
    tape = db.connect(s.tape_path, "tape", shared=True)
    store = Store(s.db_path, s.exclude_versions)
    windows = store.db.execute(
        "SELECT ticker, series, close_ts, strike, result FROM windows WHERE close_ts > ? AND close_ts <= ? AND result IN ('yes','no') AND strike > 0 ORDER BY close_ts", (start, end),
    ).fetchall()
    ticks: dict[str, tuple[list[float], list[float]]] = {}

    def feed(index: str) -> tuple[list[float], list[float]]:
        if index not in ticks:
            r = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (index, start - 1500.0, end)).fetchall()
            ticks[index] = ([float(x[0]) for x in r], [float(x[1]) for x in r])
        return ticks[index]

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
        yield {"ticker": w["ticker"], "series": series, "cls": "crypto" if crypto else "commodity", "coin": COINS.get(series) or series[2:-3], "side": side, "ts": at, "close_ts": close,
               "ask": ask, "bid": bid, "left_s": close - at, "won": bool(won), "pnl": pnl, "flip_pnl": flip, "flip_price": other, **snap}


def market_scoreboard(s: Settings, days: float = 6.0, recent_days: float = 1.0, now: float | None = None) -> dict[tuple[str, str, str, str], dict]:
    """favorites() totalled by (class, market, side, variety): the count, the wins (a loss settles at zero) and the net of a 10-dollar ticket with the taker fee, all and `recent_days` alone, and the flip. This keeps the
    observation going while a lane is paused, for instance by the choppy rule."""
    now = time.time() if now is None else now
    keys = ("n", "wins", "net", "recent_n", "recent_wins", "recent_net", "flip_wins", "flip_net", "flip_price", "recent_flip_net")
    board: dict[tuple[str, str, str, str], dict] = defaultdict(lambda: dict.fromkeys(keys, 0.0))
    for r in favorites(s, now - days * 86400.0, now - 600.0):
        cell = board[(r["cls"], r["coin"], r["side"], r["variety"])]
        cell["n"] += 1
        cell["wins"] += r["won"]
        cell["net"] += r["pnl"]
        cell["flip_wins"] += not r["won"]
        cell["flip_net"] += r["flip_pnl"]
        cell["flip_price"] += r["flip_price"]
        if r["ts"] >= now - recent_days * 86400.0:
            cell["recent_n"] += 1
            cell["recent_wins"] += r["won"]
            cell["recent_net"] += r["pnl"]
            cell["recent_flip_net"] += r["flip_pnl"]
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
    """The variety a bet would be at this moment, from the index ticks of the last five minutes, which are read from memory (ticks.history) and only from the tape when the history does not reach back that far.
    A commodity reads BiQuote against its own price when the window opened; a crypto coin reads its settlement index against the window's `strike`. None when the series has no index, the reference is missing
    (no BiQuote tick near the open, or no strike for a coin), or there are too few recent ticks: a caller that skips on a variety must not skip on None."""
    crypto = crypto_index(series)
    index = crypto or biquote_index(series)
    if not index or left_seconds <= 0:
        return None
    now = time.time() if now is None else now
    close = now + left_seconds
    load_custom(s)
    if crypto:
        if not strike or strike <= 0:
            return None
        ref: float | None = strike
    else:
        opened = close - 900.0
        ref = ticks_mod.history.first_at_or_after(index, opened - 5.0, 25.0)
        if ref is None:
            tape = db.connect(s.tape_path, "tape", shared=True)
            row = tape.execute("SELECT value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts LIMIT 1", (index, opened - 5.0, opened + 20.0)).fetchone()
            ref = float(row[0]) if row else None
        if ref is None:
            return None
    if ref is None:
        return None
    got = ticks_mod.history.window(index, now - LOOK - 40.0, now)
    if got is None:
        tape = db.connect(s.tape_path, "tape", shared=True)
        rows = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (index, now - LOOK - 40.0, now)).fetchall()
        got = ([float(r[0]) for r in rows], [float(r[1]) for r in rows])
    snap = snapshot(got[0], got[1], ref, side, now, close)
    return snap["variety"] if snap else None


def skipped(varieties: Sequence[str], variety: str | None, side: str) -> bool:
    """Whether a lane's skip list names this variety: "G" skips it on either side, "A-yes" only on that side."""
    return variety is not None and (variety in varieties or f"{variety}-{side}" in varieties)


CASE_FIELDS = ("ticker", "series", "cls", "coin", "side", "ts", "close_ts", "ask", "bid", "left_s", "won", "pnl", "flip_pnl", "z", "slope", "crosses", "vol_ratio", "since_cross", "variety")
LEDGER_KEEP = 72 * 3600.0  # how much settled history the in-memory ledger holds
LEDGER_WARM = 48 * 3600.0  # how far back a cold start computes when favorite_cases holds nothing


class Ledger:
    """Every settled favorite of the last LEDGER_KEEP seconds, in memory, so the question "what does this variety pay right now" is answered from a list and never from a query.

    Rows arrive from refresh_ledger() (a background job, new windows only) and are also saved to the favorite_cases table with their whole feature vector: that table is the history a regression can be fitted on later,
    and it is what a restart reloads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.rows: deque[dict] = deque()
        self.seen: set[str] = set()
        self.last_close = 0.0

    def add(self, recs: Sequence[dict]) -> list[dict]:
        new = []
        with self._lock:
            for r in sorted(recs, key=lambda r: r["close_ts"]):
                if r["ticker"] in self.seen:
                    continue
                self.seen.add(r["ticker"])
                self.rows.append(r)
                self.last_close = max(self.last_close, r["close_ts"])
                new.append(r)
            cutoff = self.last_close - LEDGER_KEEP
            while self.rows and self.rows[0]["close_ts"] < cutoff:
                self.seen.discard(self.rows.popleft()["ticker"])
        return new

    def cells(self, start: float, end: float, cls: str | None = None) -> dict[tuple[str, str, str], tuple[int, float]]:
        """(class, side, variety) -> (count, net $ of a $10 ticket each) for favorites that closed in (start, end]."""
        out: dict[tuple[str, str, str], list[float]] = defaultdict(lambda: [0, 0.0])
        with self._lock:
            for r in self.rows:
                if start < r["close_ts"] <= end and (cls is None or r["cls"] == cls):
                    c = out[(r["cls"], r["side"], r["variety"])]
                    c[0] += 1
                    c[1] += r["pnl"]
        return {k: (int(n), net) for k, (n, net) in out.items()}

    def skips(self, cls: str, now: float, hours: float = 6.0, prior_weight: float = 15.0, min_loss: float = 0.10, min_n: int = 5) -> tuple[str, ...]:
        """The "variety-side" cells a lane should not buy right now.

        A cell's recent record (the last `hours`) is blended with its longer record (everything older in the ledger) as a prior worth `prior_weight` bets, so a handful of recent bets can move a cell but not flip it
        on noise. It is skipped when the blend loses `min_loss` or more a $10 bet and the recent window holds at least `min_n` of its favorites. A cell with no longer record is judged on the recent window alone."""
        cut = now - hours * 3600.0
        recent = self.cells(cut, now, cls)
        older = self.cells(0.0, cut, cls)
        out = []
        for key, (n, net) in sorted(recent.items()):
            if n < min_n:
                continue
            m_recent = net / n
            n_old, net_old = older.get(key, (0, 0.0))
            post = (n * m_recent + prior_weight * (net_old / n_old)) / (n + prior_weight) if n_old else m_recent
            if post <= -min_loss:
                out.append(f"{key[2]}-{key[1]}")
        return tuple(out)

    def market_skip(self, cls: str, coin: str, ask: float, now: float, hours: float = 48.0, prior_weight: float = 30.0, min_loss: float = 0.30, min_n: int = 20, cell_min_n: int = 12, cell_min_loss: float = 0.40) -> str | None:
        """Why a favorite of this market at this price should not be bought right now, or None.

        Varieties pool every market of a class, but the markets differ: over 2026-11-02/04 Brent favorites beat their price by 5.8 points and palladium's lost to it by 6.3. So the ledger is also read per market, over a longer
        stretch (`hours`, a market's quality moves slowly): the market's mean result a $10 bet is blended with its class's as a prior worth `prior_weight` bets, and the market is skipped when that blend loses `min_loss` or more
        (and it has `min_n` favorites). Then the same is done for the market's price band (market_band), blended toward the market's own blend: a band that loses `cell_min_loss` or more is skipped even when the market is not."""
        start = now - hours * 3600.0
        with self._lock:
            rows = [r for r in self.rows if start < r["close_ts"] <= now and r["cls"] == cls and "coin" in r and "ask" in r]
        if len(rows) < min_n:
            return None
        class_mean = sum(r["pnl"] for r in rows) / len(rows)
        mine = [r for r in rows if r["coin"] == coin]
        coin_post = class_mean
        if mine:
            coin_post = (sum(r["pnl"] for r in mine) + prior_weight * class_mean) / (len(mine) + prior_weight)
            if len(mine) >= min_n and coin_post <= -min_loss:
                return f"{coin} has lost {coin_post:+.2f} a $10 favorite over the last {hours:g}h ({len(mine)} favorites, class mean {class_mean:+.2f})"
        band = market_band(ask)
        cell = [r for r in mine if market_band(r["ask"]) == band]
        if len(cell) >= cell_min_n:
            cell_post = (sum(r["pnl"] for r in cell) + prior_weight * coin_post) / (len(cell) + prior_weight)
            if cell_post <= -cell_min_loss:
                return f"{coin} favorites priced {band} have lost {cell_post:+.2f} a $10 bet over the last {hours:g}h ({len(cell)} favorites)"
        return None

    def __len__(self) -> int:
        return len(self.rows)


ledger = Ledger()


def market_band(ask: float) -> str:
    """The price band a favorite's cell is judged in."""
    return ".78-.82" if ask < 0.82 else ".82-.87" if ask < 0.87 else ".87+"


def coin_of(series: str) -> str:
    """The market name the ledger files a series under (BTC, GOLD, 30YRRATE...)."""
    return COINS.get(series) or series[2:-3]


def _ensure_cases(store: Store) -> None:
    store.db.execute(
        "CREATE TABLE IF NOT EXISTS favorite_cases (ticker TEXT PRIMARY KEY, series TEXT, cls TEXT, coin TEXT, side TEXT, ts REAL, close_ts REAL, ask REAL, bid REAL, left_s REAL, won INTEGER, pnl REAL, "
        "flip_pnl REAL, z REAL, slope REAL, crosses INTEGER, vol_ratio REAL, since_cross REAL, variety TEXT)"
    )
    store.db.execute("CREATE INDEX IF NOT EXISTS idx_favorite_cases_close ON favorite_cases(close_ts)")


def refresh_ledger(s: Settings, now: float | None = None) -> int:
    """Bring the ledger up to date, in a background job: reload what favorite_cases holds on a cold start, then score the windows settled since (with a look-back for results that arrive late) and save them.
    Returns how many favorites were added."""
    now = time.time() if now is None else now
    store = Store(s.db_path, s.exclude_versions)
    _ensure_cases(store)
    if not len(ledger):
        rows = store.db.execute(f"SELECT {', '.join(CASE_FIELDS)} FROM favorite_cases WHERE close_ts > ? ORDER BY close_ts", (now - LEDGER_KEEP,)).fetchall()
        ledger.add([{**{k: r[k] for k in CASE_FIELDS}, "won": bool(r["won"])} for r in rows])
    start = (ledger.last_close - 1800.0) if ledger.last_close else now - LEDGER_WARM
    added = ledger.add(list(favorites(s, start, now - 600.0)))
    for r in added:
        store.db.execute(f"INSERT OR IGNORE INTO favorite_cases ({', '.join(CASE_FIELDS)}) VALUES ({', '.join('?' * len(CASE_FIELDS))})", tuple(int(r[k]) if k in ("won", "crosses") else r[k] for k in CASE_FIELDS))
    if added:
        store.db.commit()
    return len(added)
