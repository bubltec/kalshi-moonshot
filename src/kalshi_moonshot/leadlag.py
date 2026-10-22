"""Does a Kalshi 15-minute quote follow its underlying instantly, or a few seconds late?

Three views of the same tape (see tape.py), all on a 1-second grid:

  correlation   feed moves against quote moves at different lags. If the quote trails the feed the
                correlation peaks at a positive lag: the feed moved first and the quote followed.
  event study   after a jump in the feed (a move of several standard deviations in 5 seconds), how far
                does the quote drift over the next 1 to 60 seconds, in the direction of the jump,
                compared with the same measurement at random moments (the drift every market has)?
  trade test    buy the side the jump favors once we could realistically react, sell it back later, and
                count fees and the spread. This is the only number that says whether the gap is
                something to trade. The same trade at random moments is the control.

Nothing here places an order.
"""

from __future__ import annotations

import bisect
import math
import random
import statistics
from dataclasses import dataclass
from typing import Any

from .commodities import instrument_for, load_instruments
from .kalshiws import index_for_series
from .strategy import fee_per_contract

HORIZONS = (1, 3, 5, 10, 20, 30, 60)
LAGS = tuple(range(-5, 31))


@dataclass(frozen=True)
class Track:
    """One market's quote path and its underlying's price path on a common 1-second grid."""

    ticker: str
    t0: int
    bid: list[float]
    ask: list[float]
    feed: list[float]

    @property
    def mid(self) -> list[float]:
        return [(b + a) / 2 for b, a in zip(self.bid, self.ask, strict=True)]


def _ffill(times: list[float], values: list[float], grid: list[float]) -> list[float | None]:
    out: list[float | None] = []
    for t in grid:
        i = bisect.bisect_right(times, t) - 1
        out.append(values[i] if i >= 0 and t - times[i] < 30 else None)  # a value older than 30s is stale
    return out


def build_tracks(db: Any, series: str, data_dir=None, min_points: int = 60) -> list[Track]:
    """One Track per market of the series that has quotes and a feed over the same stretch."""
    index = index_for_series(series)
    if index:
        rows = db.execute("SELECT ts, value FROM index_ticks WHERE index_id=? ORDER BY ts", (index,)).fetchall()
    else:
        inst = instrument_for(series, load_instruments(data_dir))
        if inst is None:
            return []
        rows = db.execute("SELECT ts, px FROM feed_ticks WHERE symbol=? ORDER BY ts", (inst.symbol,)).fetchall()
    if len(rows) < min_points:
        return []
    ft = [r[0] for r in rows]
    fv = [r[1] for r in rows]
    tracks: list[Track] = []
    tickers = [r[0] for r in db.execute("SELECT DISTINCT ticker FROM quotes WHERE ticker LIKE ?", (series + "-%",))]
    for ticker in tickers:
        q = db.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? ORDER BY ts", (ticker,)).fetchall()
        if len(q) < 10:
            continue
        start, end = int(max(q[0][0], ft[0])) + 1, int(min(q[-1][0], ft[-1]))
        if end - start < min_points:
            continue
        grid = [float(t) for t in range(start, end)]
        qt = [r[0] for r in q]
        bid, ask, feed = _ffill(qt, [r[1] for r in q], grid), _ffill(qt, [r[2] for r in q], grid), _ffill(ft, fv, grid)
        keep = [i for i in range(len(grid)) if bid[i] is not None and ask[i] is not None and feed[i] is not None]
        if len(keep) < min_points:
            continue
        # keep only the longest unbroken stretch so lags never straddle a gap
        runs, cur = [], [keep[0]]
        for i in keep[1:]:
            if i == cur[-1] + 1:
                cur.append(i)
            else:
                runs.append(cur)
                cur = [i]
        runs.append(cur)
        best = max(runs, key=len)
        if len(best) >= min_points:
            tracks.append(Track(ticker, start + best[0], [bid[i] for i in best],  # type: ignore[misc]
                                [ask[i] for i in best], [feed[i] for i in best]))  # type: ignore[misc]
    return tracks


def _corr(a: list[float], b: list[float]) -> float:
    if len(a) < 30 or len(a) != len(b):
        return float("nan")
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return num / den if den > 0 else float("nan")


def correlation_by_lag(tracks: list[Track], lags: tuple[int, ...] = LAGS) -> dict[int, float]:
    """Correlation of the feed's 1-second return with the quote's change `lag` seconds later."""
    out: dict[int, float] = {}
    for lag in lags:
        xs: list[float] = []
        ys: list[float] = []
        for t in tracks:
            m = t.mid
            r = [math.log(t.feed[i] / t.feed[i - 1]) for i in range(1, len(m))]
            dm = [m[i] - m[i - 1] for i in range(1, len(m))]
            n = len(r)
            for i in range(max(0, -lag), min(n, n - lag)):
                xs.append(r[i])
                ys.append(dm[i + lag])
        out[lag] = _corr(xs, ys)
    return out


def jumps(track: Track, z: float = 2.5, min_bp: float = 1.5, window: int = 5, cooldown: int = 10) -> list[tuple[int, int]]:
    """(index, direction) where the feed moved at least z standard deviations in `window` seconds."""
    f = track.feed
    rets = [math.log(f[i] / f[i - window]) for i in range(window, len(f))]
    if len(rets) < 30:
        return []
    sd = statistics.pstdev(rets) or 1e-12
    thresh = max(z * sd, min_bp / 1e4)
    out, last = [], -10**9
    for k, r in enumerate(rets):
        i = k + window
        if abs(r) >= thresh and i - last >= cooldown:
            out.append((i, 1 if r > 0 else -1))
            last = i
    return out


@dataclass(frozen=True)
class Response:
    horizon: int
    n: int
    drift: float  # mean quote move in the jump's direction, in dollars of contract price
    control: float  # the same measurement at random moments (random direction)


def event_study(tracks: list[Track], horizons: tuple[int, ...] = HORIZONS, seed: int = 7) -> list[Response]:
    rng = random.Random(seed)
    out = []
    for h in horizons:
        moves: list[float] = []
        control: list[float] = []
        for t in tracks:
            m = t.mid
            for i, d in jumps(t):
                if i + h < len(m):
                    moves.append((m[i + h] - m[i]) * d)
            for _ in range(len(jumps(t))):
                i = rng.randrange(0, max(1, len(m) - h))
                if i + h < len(m):
                    control.append((m[i + h] - m[i]) * rng.choice((-1, 1)))
        out.append(Response(h, len(moves), statistics.fmean(moves) if moves else float("nan"),
                            statistics.fmean(control) if control else float("nan")))
    return out


@dataclass(frozen=True)
class TradeTest:
    hold: int
    n: int
    avg_cents: float  # mean profit per contract, after fees and crossing the spread
    hit: float
    control_cents: float  # the same trade at random moments in random directions


def _trade(t: Track, i: int, d: int, latency: int, hold: int) -> float | None:
    a, b = i + latency, i + latency + hold
    if b >= len(t.bid):
        return None
    if d > 0:
        entry, exit_ = t.ask[a], t.bid[b]
    else:
        entry, exit_ = 1 - t.bid[a], 1 - t.ask[b]
    if not 0.10 <= entry <= 0.90 or t.ask[a] - t.bid[a] > 0.05:
        return None
    return exit_ - entry - fee_per_contract(entry) - fee_per_contract(max(exit_, 0.0))


def trade_test(tracks: list[Track], latency: int = 2, holds: tuple[int, ...] = (5, 15, 30, 60), seed: int = 11) -> list[TradeTest]:
    rng = random.Random(seed)
    out = []
    for hold in holds:
        pnl: list[float] = []
        ctl: list[float] = []
        for t in tracks:
            for i, d in jumps(t):
                r = _trade(t, i, d, latency, hold)
                if r is not None:
                    pnl.append(r)
            for _ in range(len(jumps(t))):
                r = _trade(t, rng.randrange(0, max(1, len(t.bid) - hold - latency - 1)), rng.choice((-1, 1)), latency, hold)
                if r is not None:
                    ctl.append(r)
        out.append(TradeTest(hold, len(pnl), 100 * statistics.fmean(pnl) if pnl else float("nan"),
                             sum(1 for x in pnl if x > 0) / len(pnl) if pnl else float("nan"),
                             100 * statistics.fmean(ctl) if ctl else float("nan")))
    return out
