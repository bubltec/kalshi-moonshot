"""Rebuild your trades from raw fills: what was bought, how it ended, and what it made.

Kalshi records each fill from the YES book's side: `bid` bought YES at yes_price, `ask` sold YES, which is
the same as buying NO at no_price. So a market's position is one signed number (YES held minus NO held),
the way the exchange nets it. A trip starts when that goes from flat to a position and ends when it returns
to flat (a cash-out) or the market settles. Closing fills are matched to opening ones first-in, first-out.

Profit and loss is in dollars after every fee on the way. A trip is flagged `reliable` unless a settlement
reports a different position than the fills add up to (usually a position opened before the history starts).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .journal import Trade
from .store import Store

EPS = 1e-9


@dataclass
class Trip:
    ticker: str
    series: str
    source: str  # "manual", "bot" or "mixed"
    side: str  # the contract first bought: "yes" or "no"
    opened_ts: float
    closed_ts: float | None = None
    contracts: float = 0.0  # bought over the trip
    cost: float = 0.0  # dollars paid for them
    gross: float = 0.0  # profit before fees
    fees: float = 0.0
    entry_value: float = 0.0  # sum of contracts * price paid, in the side's own price
    exit_value: float = 0.0  # sum of contracts * price received (or settlement value)
    exit_contracts: float = 0.0
    cashed_out: float = 0.0  # contracts closed by selling before settlement
    settled: float = 0.0  # contracts closed by settlement
    result: str | None = None
    reliable: bool = True
    close_ts: float | None = None  # when the market closes (for the minute within a 15-minute window)
    order_ids: set[str] = field(default_factory=set)

    @property
    def pnl(self) -> float:
        return self.gross - self.fees

    @property
    def roi(self) -> float:
        return self.pnl / self.cost if self.cost else float("nan")

    @property
    def entry_price(self) -> float:
        return self.entry_value / self.contracts if self.contracts else float("nan")

    @property
    def exit_price(self) -> float:
        return self.exit_value / self.exit_contracts if self.exit_contracts else float("nan")

    @property
    def done(self) -> bool:
        return self.closed_ts is not None

    @property
    def hold_minutes(self) -> float:
        return ((self.closed_ts or self.opened_ts) - self.opened_ts) / 60.0

    @property
    def exit_type(self) -> str:
        if self.cashed_out > EPS and self.settled > EPS:
            return "mixed"
        return "cash-out" if self.cashed_out > EPS else "settlement"

    def minute_in_window(self, when: float | None, minutes: float = 15.0) -> float | None:
        """How many minutes into a 15-minute window a time falls, or None for other series."""
        if when is None or self.close_ts is None or not self.series.endswith("15M"):
            return None
        return minutes - (self.close_ts - when) / 60.0

    def as_trade(self) -> Trade:
        return Trade(self.ticker, self.source, self.series, self.side, True, self.cost, self.cost + self.pnl,
                     self.closed_ts or self.opened_ts, self.exit_type == "cash-out")


def _label(ids: set[str], bot_ids: set[str]) -> str:
    mine = ids & bot_ids
    return "bot" if ids and mine == ids else "manual" if not mine else "mixed"


def build_trips(store: Store) -> list[Trip]:
    db = store.db
    bot_ids = {r[0] for r in db.execute("SELECT order_id FROM orders WHERE order_id IS NOT NULL AND order_id != ''")}
    closes = {r["ticker"]: r["close_ts"] for r in db.execute("SELECT ticker, close_ts FROM market_info")}
    settle = {r["ticker"]: r for r in db.execute("SELECT * FROM settlements ORDER BY settled_ts")}
    by_ticker: dict[str, list] = defaultdict(list)
    for r in db.execute("SELECT * FROM fills ORDER BY ts, fill_id"):
        by_ticker[r["ticker"]].append(r)
    trips: list[Trip] = []
    for ticker, fills in by_ticker.items():
        lots: list[list[float]] = []  # [sign, qty, yes_price]
        trip: Trip | None = None

        def finish(trip_: Trip, when: float) -> None:
            trip_.closed_ts = when
            trip_.source = _label(trip_.order_ids, bot_ids)
            trips.append(trip_)

        for f in fills:
            sign = 1.0 if f["book_side"] == "bid" else -1.0
            n, p = float(f["count"]), float(f["yes_price"])
            if trip is None:
                trip = Trip(ticker, ticker.split("-")[0], "manual", "yes" if sign > 0 else "no", f["ts"],
                            close_ts=closes.get(ticker))
            if f["order_id"]:
                trip.order_ids.add(f["order_id"])
            trip.fees += float(f["fee"] or 0.0)
            while n > EPS and lots and lots[0][0] != sign:
                lot = lots[0]
                m = min(n, lot[1])
                trip.gross += m * (p - lot[2]) * lot[0]
                trip.exit_value += m * (p if lot[0] > 0 else 1.0 - p)
                trip.exit_contracts += m
                trip.cashed_out += m
                lot[1] -= m
                n -= m
                if lot[1] <= EPS:
                    lots.pop(0)
            if n > EPS:
                lots.append([sign, n, p])
                trip.contracts += n
                trip.cost += n * (p if sign > 0 else 1.0 - p)
                trip.entry_value += n * (p if sign > 0 else 1.0 - p)
            if not lots:
                finish(trip, f["ts"])
                trip = None
        if trip is None:
            continue
        s = settle.get(ticker)
        if s is not None and s["result"] in {"yes", "no"}:
            yes_value = 1.0 if s["result"] == "yes" else 0.0
            held = sum(lot[0] * lot[1] for lot in lots)
            reported = float(s["yes_count"] or 0.0) - float(s["no_count"] or 0.0)
            trip.reliable = abs(held - reported) < 0.01
            for sign_, qty, price in lots:
                trip.gross += qty * (yes_value - price) * sign_
                trip.exit_value += qty * (yes_value if sign_ > 0 else 1.0 - yes_value)
                trip.exit_contracts += qty
                trip.settled += qty
            trip.fees += float(s["fee"] or 0.0)
            trip.result = s["result"]
            finish(trip, s["settled_ts"])
        else:
            trip.source = _label(trip.order_ids, bot_ids)
            trips.append(trip)  # still open
    trips.sort(key=lambda t: t.opened_ts)
    return trips


# ---------------------------------------------------------------- views over finished trips


def finished(trips: list[Trip], source: str | None = None, suffix: str | None = None) -> list[Trip]:
    return [
        t for t in trips
        if t.done and t.reliable and (source in (None, "all") or t.source == source)
        and (suffix is None or t.series.endswith(suffix))
    ]


@dataclass(frozen=True)
class Row:
    label: str
    n: int
    wins: int
    staked: float
    pnl: float
    extra: float = float("nan")  # a second number a view wants to show (median hold, mean entry price, ...)

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else float("nan")

    @property
    def roi(self) -> float:
        return self.pnl / self.staked if self.staked else float("nan")


def _row(label: str, items: list[Trip], extra: float = float("nan")) -> Row:
    return Row(label, len(items), sum(1 for t in items if t.pnl > 0), sum(t.cost for t in items), sum(t.pnl for t in items), extra)


def _median(xs: list[float]) -> float:
    xs = sorted(x for x in xs if x == x)
    return xs[len(xs) // 2] if xs else float("nan")


def by_series(items: list[Trip]) -> list[Row]:
    groups: dict[str, list[Trip]] = defaultdict(list)
    for t in items:
        groups[t.series].append(t)
    return sorted((_row(k, v) for k, v in groups.items()), key=lambda r: r.pnl, reverse=True)


def by_exit(items: list[Trip]) -> list[Row]:
    groups: dict[str, list[Trip]] = defaultdict(list)
    for t in items:
        groups[t.exit_type].append(t)
    return [_row(k, v, _median([t.hold_minutes for t in v])) for k, v in sorted(groups.items())]


ENTRY_MINUTES = ((0, 3), (3, 6), (6, 9), (9, 12), (12, 15))
ENTRY_PRICES = ((0.0, 0.30), (0.30, 0.70), (0.70, 1.01))


def entry_minute_table(items: list[Trip]) -> list[Row]:
    """15-minute trips grouped by how far into the window the first buy happened."""
    out = []
    for lo, hi in ENTRY_MINUTES:
        g = [t for t in items if (m := t.minute_in_window(t.opened_ts)) is not None and lo <= m < hi]
        if g:
            out.append(_row(f"minutes {lo}-{hi}", g, sum(t.entry_price for t in g) / len(g)))
    return out


def entry_price_table(items: list[Trip]) -> list[Row]:
    out = []
    for lo, hi in ENTRY_PRICES:
        g = [t for t in items if lo <= t.entry_price < hi]
        if g:
            out.append(_row(f"paid {lo:.2f}-{min(hi, 1.0):.2f}", g, _median([t.hold_minutes for t in g])))
    return out


def cashout_multiples(items: list[Trip]) -> list[float]:
    """Sale price over purchase price for trips that were cashed out (2.0 means sold at double)."""
    return sorted(t.exit_price / t.entry_price for t in items
                  if t.exit_type == "cash-out" and t.entry_price > 0 and t.exit_price == t.exit_price)
