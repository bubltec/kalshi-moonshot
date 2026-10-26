"""A day-trader's view of the book: dollars won and lost per day, not per-contract statistics.

Every bet the bot sizes (live or paper) becomes one Trade with a dollar result. Trades are grouped
by the day their market closed (or the bet was stopped out), in the exchange's time zone, and each
day is summarized the way a trading journal would: how many trades, how many won, dollars staked,
net profit and loss, and the size of the average win against the average loss.

Over the whole run it also reports profit factor (money won per dollar lost), expectancy per trade,
the deepest drawdown of the running total, and the longest losing streak. These say whether the
approach makes money at the size it actually trades, which a calibration score does not.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .store import Store
from .strategy import fee_per_contract

EXCHANGE_TZ = "America/New_York"


@dataclass(frozen=True)
class Trade:
    ticker: str
    lane: str
    series: str
    side: str
    live: bool
    stake: float
    returned: float
    closed_ts: float  # when it finished: market close, or the stop-loss exit
    stopped: bool = False
    opened_ts: float = 0.0  # when the bet was placed
    close_ts: float = 0.0  # when its market closes (to place the bet inside a 15-minute window)
    ver: str | None = None  # the version of its lane's logic that made the bet (None = before versions were recorded)

    @property
    def pnl(self) -> float:
        return self.returned - self.stake

    @property
    def won(self) -> bool:
        return self.pnl > 0


def _closed_ts(row: Any) -> float:
    """When the trade finished. A market can settle before its listed close, never after it."""
    if row["exit_ts"] is not None:
        return float(row["exit_ts"])
    seen = row["settled_ts"]
    try:
        listed = datetime.fromisoformat(row["close_time"]).timestamp()
    except (TypeError, ValueError):
        return float(seen or row["ts"])
    return min(listed, float(seen)) if seen else listed


def _listed_close(row: Any) -> float:
    try:
        return datetime.fromisoformat(row["close_time"]).timestamp()
    except (TypeError, ValueError):
        return 0.0


def trades(store: Store) -> list[Trade]:
    """Finished sized bets, oldest first. Open bets are not trades yet."""
    rows = store.db.execute(
        """SELECT ticker, lane, side, mode, contracts, stake_usd, outcome, exit_price, exit_ts,
                  close_time, settled_ts, ts, lane_ver
           FROM decisions
           WHERE action='bet' AND stake_usd IS NOT NULL AND contracts > 0
             AND (COALESCE(outcome,'') IN ('yes','no') OR exit_ts IS NOT NULL)"""
    ).fetchall()
    out: list[Trade] = []
    for r in rows:
        stopped = r["exit_price"] is not None
        if stopped:
            returned = r["contracts"] * (r["exit_price"] - fee_per_contract(r["exit_price"]))
        else:
            returned = float(r["contracts"]) if r["outcome"] == (r["side"] or "yes") else 0.0
        out.append(Trade(
            r["ticker"], r["lane"] or "moonshot", r["ticker"].split("-")[0], r["side"] or "yes",
            r["mode"] == "trade", float(r["stake_usd"]), returned, _closed_ts(r), stopped,
            float(r["ts"]), _listed_close(r), r["lane_ver"],
        ))
    out.sort(key=lambda t: t.closed_ts)
    return out


def cluster_se(pairs: list[tuple[Any, float]], mean: float) -> float:
    """Standard error of `mean` that respects clustering: (cluster key, value) pairs whose key matches move together.

    Bets in one 15-minute slot (several coins, both sides, twin lanes) are one event wearing several tickers, so the
    plain standard error over bets understates the uncertainty, badly when a rule fires on many coins at once.
    This is the CR1 cluster-robust error: each cluster contributes its summed deviation from the mean, and the
    spread of those sums (scaled by G/(G-1)) is the variance of the mean. With fewer than two clusters it falls
    back to the plain error over the values.
    """
    import math

    n = len(pairs)
    if n < 2:
        return float("inf")
    groups: dict[Any, float] = {}
    for key, value in pairs:
        groups[key] = groups.get(key, 0.0) + (value - mean)
    if len(groups) < 2:
        var = sum((v - mean) ** 2 for _, v in pairs) / (n - 1)
        return math.sqrt(var / n)
    g = len(groups)
    return math.sqrt((g / (g - 1.0)) * sum(s * s for s in groups.values()) / (n * n))


def day_of(ts: float, tz: str = EXCHANGE_TZ) -> str:
    return datetime.fromtimestamp(ts, UTC).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class Day:
    day: str
    n: int
    wins: int
    staked: float
    pnl: float
    best: float
    worst: float

    @property
    def roi(self) -> float:
        return self.pnl / self.staked if self.staked else float("nan")


def by_day(items: list[Trade], tz: str = EXCHANGE_TZ) -> list[Day]:
    groups: dict[str, list[Trade]] = defaultdict(list)
    for t in items:
        groups[day_of(t.closed_ts, tz)].append(t)
    return [
        Day(d, len(g), sum(t.won for t in g), sum(t.stake for t in g), sum(t.pnl for t in g),
            max(t.pnl for t in g), min(t.pnl for t in g))
        for d, g in sorted(groups.items())
    ]


@dataclass(frozen=True)
class Summary:
    trades: int
    wins: int
    staked: float
    pnl: float
    avg_win: float
    avg_loss: float
    profit_factor: float  # dollars won per dollar lost; above 1 is profitable
    expectancy: float  # average dollars per trade
    max_drawdown: float  # deepest fall of the running total from its peak, in dollars
    worst_streak: int  # most losing trades in a row
    green_days: int
    days: int

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else float("nan")

    @property
    def payoff(self) -> float:
        """Average win over average loss. With a high win rate this is small, and one loss is many wins."""
        return self.avg_win / self.avg_loss if self.avg_loss else float("nan")

    @property
    def breakeven_win_rate(self) -> float:
        """The win rate this payoff needs just to break even."""
        return 1.0 / (1.0 + self.payoff) if self.payoff == self.payoff and self.payoff > 0 else float("nan")


def summarize(items: list[Trade], tz: str = EXCHANGE_TZ) -> Summary:
    wins = [t.pnl for t in items if t.pnl > 0]
    losses = [-t.pnl for t in items if t.pnl <= 0]
    peak = run = dd = 0.0
    streak = worst = 0
    for t in items:
        run += t.pnl
        peak = max(peak, run)
        dd = max(dd, peak - run)
        streak = streak + 1 if t.pnl <= 0 else 0
        worst = max(worst, streak)
    days = by_day(items, tz)
    return Summary(
        len(items), len(wins), sum(t.stake for t in items), sum(t.pnl for t in items),
        sum(wins) / len(wins) if wins else 0.0, sum(losses) / len(losses) if losses else 0.0,
        sum(wins) / sum(losses) if losses and sum(losses) else float("inf") if wins else float("nan"),
        sum(t.pnl for t in items) / len(items) if items else float("nan"),
        dd, worst, sum(d.pnl > 0 for d in days), len(days),
    )


def select(items: list[Trade], *, lane: str | None = None, live: bool | None = None,
           suffix: str | None = None) -> list[Trade]:
    return [
        t for t in items
        if (lane is None or t.lane == lane)
        and (live is None or t.live == live)
        and (suffix is None or t.series.endswith(suffix))
    ]


ENTRY_BANDS = ((0, 3), (3, 6), (6, 9), (9, 12), (12, 15))


def by_entry_minute(items: list[Trade], window: float = 15.0) -> list[tuple[str, int, int, float, float]]:
    """15-minute bets grouped by how far into the window they were placed: (band, bets, won, staked, profit)."""
    out = []
    for lo, hi in ENTRY_BANDS:
        g = [
            t for t in items
            if t.series.endswith("15M") and t.close_ts and t.opened_ts
            and lo <= window - (t.close_ts - t.opened_ts) / 60.0 < hi
        ]
        if g:
            out.append((f"minutes {lo}-{hi}", len(g), sum(1 for t in g if t.won), sum(t.stake for t in g), sum(t.pnl for t in g)))
    return out
