"""Intraday history of the 15-minute contracts themselves, from Kalshi's own candlestick API.

Kalshi keeps 1-minute candles (bid, ask, last price, volume, open interest) for every market,
settled ones included. `ingest` stores them locally for recent settled windows (safe to re-run: it
only fetches windows it has not seen), and `profile` turns them into day-trading statistics about how
these contracts move, not how a bet would have scored:

  path        the typical YES price at each minute of the window, and how much is already decided
  decision    how early a window is effectively over (a side at 85%+ that goes on to win)
  reversal    how often the favorite at minute k still loses, and how far it swings against you
  liquidity   spread and volume per minute, so the cost of getting in is known
  by hour     the same, split by the hour (New York time) the window opened
"""

from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .store import Store

WINDOW = 15  # minutes


def _d(block: Any, key: str = "close_dollars") -> float | None:
    v = (block or {}).get(key)
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def ingest(
    client: Any, store: Store, series: list[str], windows: int = 150, max_new: int | None = None,
    refresh: bool = False,
) -> int:
    """Store candles for recent settled windows not yet held. Returns windows added.

    max_new stops after that many windows, so a scheduled run stays short; the rest is picked up
    on the next run.
    """
    added = 0
    for ser in series:
        have = {r[0] for r in store.db.execute("SELECT ticker FROM windows WHERE series=?", (ser,))}
        if refresh:  # windows stored before the minute highs were kept get fetched again
            have -= {r[0] for r in store.db.execute(
                "SELECT DISTINCT ticker FROM candles WHERE bid_hi IS NULL"
            )}
        for m in client.settled_markets(ser, limit=windows):
            if m["ticker"] in have or m.get("result") not in {"yes", "no"}:
                continue
            if max_new is not None and added >= max_new:
                return added
            close = int(datetime.fromisoformat(m["close_time"]).timestamp())
            cs = client._request(
                "GET", f"/series/{ser}/markets/{m['ticker']}/candlesticks",
                params={"start_ts": close - WINDOW * 60, "end_ts": close, "period_interval": 1},
            ).get("candlesticks", [])
            rows = [
                (m["ticker"], int(c["end_period_ts"]), _d(c.get("yes_bid")), _d(c.get("yes_ask")),
                 _d(c.get("yes_bid"), "low_dollars"), _d(c.get("yes_ask"), "high_dollars"),
                 _d(c.get("price")), float(c.get("volume_fp") or 0), float(c.get("open_interest_fp") or 0),
                 _d(c.get("yes_bid"), "high_dollars"), _d(c.get("yes_ask"), "low_dollars"))
                for c in cs
            ]
            store.db.executemany(
                "INSERT OR REPLACE INTO candles (ticker, end_ts, bid_c, ask_c, bid_lo, ask_hi, last_c, volume,"
                " oi, bid_hi, ask_lo) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows,
            )
            store.db.execute(
                "INSERT OR REPLACE INTO windows VALUES (?,?,?,?,?,?)",
                (m["ticker"], ser, close, m.get("floor_strike"), m["result"], time.time()),
            )
            store.db.commit()
            added += 1
    return added


def prune(store: Store, keep_days: int, now: float | None = None) -> int:
    """Drop windows (and their candles) older than keep_days. Returns windows removed."""
    cutoff = int((now if now is not None else time.time()) - keep_days * 86400)
    old = [r[0] for r in store.db.execute("SELECT ticker FROM windows WHERE close_ts < ?", (cutoff,))]
    for ticker in old:
        store.db.execute("DELETE FROM candles WHERE ticker=?", (ticker,))
        store.db.execute("DELETE FROM windows WHERE ticker=?", (ticker,))
    store.db.commit()
    return len(old)


@dataclass(frozen=True)
class Win:
    ticker: str
    series: str
    close_ts: int
    result: str
    mid: dict[int, float]  # elapsed minute (1..15) -> YES mid
    spread: dict[int, float]
    volume: dict[int, float]
    fav_bid_lo: dict[int, float]  # elapsed minute -> lowest YES bid in that minute

    @property
    def open_ts(self) -> int:
        return self.close_ts - WINDOW * 60


def load(store: Store, suffix: str | None = None) -> list[Win]:
    wins = store.db.execute("SELECT ticker, series, close_ts, result FROM windows ORDER BY close_ts").fetchall()
    out: list[Win] = []
    for w in wins:
        if suffix and not w["series"].endswith(suffix):
            continue
        rows = store.db.execute(
            "SELECT end_ts, bid_c, ask_c, bid_lo, volume FROM candles WHERE ticker=? ORDER BY end_ts",
            (w["ticker"],),
        ).fetchall()
        mid: dict[int, float] = {}
        spread: dict[int, float] = {}
        vol: dict[int, float] = {}
        lo: dict[int, float] = {}
        for r in rows:
            k = WINDOW - (w["close_ts"] - r["end_ts"]) // 60
            if not 1 <= k <= WINDOW or r["bid_c"] is None or r["ask_c"] is None:
                continue
            mid[k] = (r["bid_c"] + r["ask_c"]) / 2.0
            spread[k] = r["ask_c"] - r["bid_c"]
            vol[k] = r["volume"] or 0.0
            if r["bid_lo"] is not None:
                lo[k] = r["bid_lo"]
        if mid:
            out.append(Win(w["ticker"], w["series"], w["close_ts"], w["result"], mid, spread, vol, lo))
    return out


def _med(xs: list[float]) -> float:
    return statistics.median(xs) if xs else float("nan")


def path(wins: list[Win]) -> list[tuple[int, float, float, float]]:
    """Per elapsed minute: median distance of the YES mid from 50% (how far from a toss-up the
    contract has moved), share of windows already decided (a side at 90%+), and median spread."""
    rows = []
    for k in range(1, WINDOW):
        mids = [w.mid[k] for w in wins if k in w.mid]
        if not mids:
            continue
        spreads = [w.spread[k] for w in wins if k in w.spread]
        rows.append((
            k, _med([abs(m - 0.5) for m in mids]),
            sum(1 for m in mids if m >= 0.9 or m <= 0.1) / len(mids), _med(spreads),
        ))
    return rows


@dataclass(frozen=True)
class Fav:
    n: int
    reversed_: float  # share where the side that was ahead at the entry minute lost
    avg_price: float
    adverse: float  # median fall of that side's bid below its entry price afterwards
    p10_adverse: float  # the worst tenth of windows


def favorites(wins: list[Win], k: int, lo: float = 0.70, hi: float = 0.90) -> Fav | None:
    """Windows where one side was priced in [lo, hi] at elapsed minute k: how often it lost, and how
    far its price swung against a holder before the window ended."""
    lost = 0
    prices: list[float] = []
    swings: list[float] = []
    for w in wins:
        m = w.mid.get(k)
        if m is None:
            continue
        side_yes = m >= 0.5
        price = m if side_yes else 1.0 - m
        if not lo <= price <= hi:
            continue
        prices.append(price)
        lost += (w.result == "yes") != side_yes
        later = [w.fav_bid_lo[j] if side_yes else None for j in range(k + 1, WINDOW + 1) if j in w.fav_bid_lo]
        if side_yes:
            if later:
                swings.append(max(0.0, price - min(x for x in later if x is not None)))
        else:
            highs = [w.mid[j] for j in range(k + 1, WINDOW + 1) if j in w.mid]
            if highs:
                swings.append(max(0.0, max(highs) - m))  # YES rising is the NO holder's loss
    if not prices:
        return None
    swings.sort()
    return Fav(len(prices), lost / len(prices), statistics.mean(prices),
               _med(swings), swings[min(len(swings) - 1, math.ceil(0.9 * len(swings)) - 1)] if swings else float("nan"))


def decision_minute(wins: list[Win], level: float = 0.85) -> list[tuple[int, float]]:
    """When a window is effectively over: the first minute a side is at `level`+ and goes on to win.
    Returns (minute, share of windows decided by then)."""
    firsts: list[int] = []
    for w in wins:
        for k in range(1, WINDOW):
            m = w.mid.get(k)
            if m is None:
                continue
            if (m >= level and w.result == "yes") or (m <= 1 - level and w.result == "no"):
                firsts.append(k)
                break
    n = len(wins)
    return [(k, sum(1 for f in firsts if f <= k) / n) for k in (3, 5, 7, 9, 10, 11, 12, 13)] if n else []


def by_hour(wins: list[Win], tz: str = "America/New_York", k: int = 10) -> list[dict[str, float]]:
    groups: dict[int, list[Win]] = defaultdict(list)
    zone = ZoneInfo(tz)
    for w in wins:
        groups[datetime.fromtimestamp(w.open_ts, UTC).astimezone(zone).hour].append(w)
    out = []
    for h, g in sorted(groups.items()):
        moves = [abs(w.mid[k] - w.mid[5]) for w in g if k in w.mid and 5 in w.mid]
        fav = favorites(g, k)
        out.append({
            "hour": h, "n": len(g), "move": _med(moves),
            "decided": sum(1 for w in g if k in w.mid and (w.mid[k] >= 0.9 or w.mid[k] <= 0.1)) / len(g),
            "reversal": fav.reversed_ if fav else float("nan"), "fav_n": fav.n if fav else 0,
            "spread": _med([w.spread[k] for w in g if k in w.spread]),
            "volume": _med([sum(w.volume.get(j, 0.0) for j in range(k - 2, k + 3)) for w in g]),
        })
    return out
