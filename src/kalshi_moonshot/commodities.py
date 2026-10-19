"""Commodity price-threshold markets (gold, silver, WTI, Brent, ...), priced with math, not a language model.

Kalshi lists a ladder of "will X close above K" contracts for each day or week. The model-free
way to rate one is a lognormal price distribution: P(above K) = Phi(ln(center / K) / sigma).

Two inputs, kept separate on purpose:

  center  Where the price is centered at close. Taken from the ladder itself (a probit fit over
          the liquid strikes), because Kalshi's reference contract differs from any free feed by
          a basis (a futures premium, a different contract month) that would swamp the edge. The
          spot feed is only a sanity check: a center far from spot means the symbol mapping is
          wrong for that series, and the event is skipped.
  sigma   How wide the distribution is. Taken from realized volatility of the spot feed, not
          from the ladder. The edge, if any, is where the ladder's own implied width disagrees
          with how much the price has actually been moving (usually the cheap tails).

Everything except YahooFeed is pure and unit tested.
"""

from __future__ import annotations

import math
import re
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from importlib import resources
from pathlib import Path
from statistics import NormalDist
from typing import Any, Protocol

import httpx

from .config import Settings

_N = NormalDist()


@dataclass(frozen=True)
class Instrument:
    prefix: str
    symbol: str
    suffixes: tuple[str, ...]


@lru_cache(maxsize=1)
def _packaged() -> tuple[Instrument, ...]:
    text = resources.files(__package__).joinpath("instruments.toml").read_text()
    return _parse_instruments(text)


def load_instruments(data_dir: Path | None = None) -> tuple[Instrument, ...]:
    """Supported series, from instruments.toml (a copy in data_dir wins over the packaged one)."""
    override = data_dir / "instruments.toml" if data_dir else None
    if override and override.is_file():
        return _parse_instruments(override.read_text())
    return _packaged()


def _parse_instruments(text: str) -> tuple[Instrument, ...]:
    raw = tomllib.loads(text)
    return tuple(
        Instrument(row["prefix"], row["symbol"], tuple(row.get("suffixes", [""])))
        for row in raw.get("instrument", [])
    )


@lru_cache(maxsize=4)
def _patterns(instruments: tuple[Instrument, ...]) -> tuple[tuple[re.Pattern[str], str], ...]:
    return tuple(
        (re.compile(rf"^{re.escape(i.prefix)}({'|'.join(map(re.escape, i.suffixes))})$"), i.symbol)
        for i in instruments
    )


def symbol_for(series: str, instruments: tuple[Instrument, ...] | None = None) -> str | None:
    """Feed symbol for a Kalshi series, or None when the series is not a supported commodity."""
    for pattern, symbol in _patterns(instruments or load_instruments()):
        if pattern.match(series or ""):
            return symbol
    return None


BAR_SECONDS = 300  # feed bar size
SESSION_GAP = 15 * 60  # bigger gap between bars = market closed, not a price move
STALE_SECONDS = 20 * 60  # a last bar older than this means the market is shut or the feed is down
OPEN_FRACTION = 23 / 24  # share of a day futures trade, used for short horizons


class CommodityError(RuntimeError):
    """Cannot price this market right now (no feed, stale feed, unusable ladder). Not a pass."""


# ---------------------------------------------------------------- price feed


@dataclass(frozen=True)
class Series:
    last: float
    last_ts: float
    closes: list[float]
    times: list[float]


class Feed(Protocol):
    def series(self, symbol: str) -> Series: ...


class YahooFeed:
    """5-minute bars for the last 5 days from Yahoo Finance's chart endpoint (no key)."""

    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    def __init__(self, ttl: float = 60.0):
        self.ttl = ttl
        self._http = httpx.Client(timeout=10.0, headers={"User-Agent": "Mozilla/5.0"})
        self._cache: dict[str, tuple[float, Series]] = {}

    def series(self, symbol: str) -> Series:
        hit = self._cache.get(symbol)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        try:
            resp = self._http.get(
                self.URL.format(symbol=symbol), params={"interval": "5m", "range": "5d"}
            )
            resp.raise_for_status()
            out = parse_chart(resp.json())
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise CommodityError(f"price feed failed for {symbol}: {exc}") from exc
        self._cache[symbol] = (time.time(), out)
        return out

    def close(self) -> None:
        self._http.close()


def parse_chart(body: dict[str, Any]) -> Series:
    result = body["chart"]["result"][0]
    closes = result["indicators"]["quote"][0]["close"]
    stamps = result["timestamp"]
    pairs = [(float(t), float(c)) for t, c in zip(stamps, closes, strict=True) if c is not None and c > 0]
    if len(pairs) < 30:
        raise ValueError(f"only {len(pairs)} bars")
    return Series(
        last=pairs[-1][1], last_ts=pairs[-1][0],
        closes=[c for _, c in pairs], times=[t for t, _ in pairs],
    )


# ---------------------------------------------------------------- the math


def prob_above(center: float, strike: float, sigma: float) -> float:
    """P(price at close > strike) when ln(price) is normal with median `center` and std `sigma`."""
    if strike <= 0:
        return 1.0
    if sigma <= 0:
        return 1.0 if center > strike else 0.0
    return _N.cdf(math.log(center / strike) / sigma)


def realized_sigma(series: Series, hours: float) -> float:
    """Std of ln(price at close / price now), scaled from recent 5-minute realized volatility.

    Returns inside a session only (a weekend gap is not a 5-minute move). Short horizons assume
    the market trades OPEN_FRACTION of the clock; longer ones use the share seen in the sample.
    """
    sq: list[float] = []
    traded = 0.0
    for i in range(1, len(series.closes)):
        gap = series.times[i] - series.times[i - 1]
        if gap <= 0 or gap > SESSION_GAP:
            continue
        r = math.log(series.closes[i] / series.closes[i - 1])
        sq.append(r * r)
        traded += gap
    if len(sq) < 20:
        raise CommodityError("not enough recent bars to measure volatility")
    per_bar = math.sqrt(sum(sq) / len(sq))
    span = series.times[-1] - series.times[0]
    share = traded / span if span > 0 else OPEN_FRACTION
    if hours <= 24:
        share = OPEN_FRACTION
    bars = max(hours, 0.0) * share * 3600.0 / BAR_SECONDS
    return per_bar * math.sqrt(bars)


@dataclass(frozen=True)
class Fit:
    center: float  # price where the ladder says P(above) = 50%
    sigma: float  # width the ladder implies (std of ln price)
    points: int
    r2: float


def _threshold(m: dict[str, Any]) -> float | None:
    for key in ("floor_strike",) if m.get("strike_type") != "less" else ("cap_strike", "floor_strike"):
        v = m.get(key)
        if v is not None:
            return float(v)
    return None


def _f(m: dict[str, Any], key: str) -> float:
    try:
        return float(m.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def fit_ladder(markets: list[dict[str, Any]], max_spread: float = 0.06) -> Fit | None:
    """Fit ln(price) ~ normal to the event's 'above K' contracts. None when the ladder is too thin.

    Uses mid prices of two-sided, tight quotes between 5% and 95%, regressing probit(mid) on
    ln(strike). Quotes outside that band are mostly the tick floor and ceiling, not information.
    """
    xs: list[float] = []
    zs: list[float] = []
    for m in markets:
        if m.get("strike_type") not in {"greater", "greater_or_equal"}:
            continue
        k = _threshold(m)
        bid, ask = _f(m, "yes_bid_dollars"), _f(m, "yes_ask_dollars")
        if not k or k <= 0 or bid <= 0 or ask >= 1.0 or ask - bid > max_spread:
            continue
        mid = (bid + ask) / 2.0
        if not 0.05 < mid < 0.95:
            continue
        xs.append(math.log(k))
        zs.append(_N.inv_cdf(mid))
    n = len(xs)
    if n < 4:
        return None
    mx, mz = sum(xs) / n, sum(zs) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    b = sum((x - mx) * (z - mz) for x, z in zip(xs, zs, strict=True)) / sxx
    if b >= 0:
        return None
    a = mz - b * mx
    sst = sum((z - mz) ** 2 for z in zs)
    sse = sum((z - (a + b * x)) ** 2 for x, z in zip(xs, zs, strict=True))
    r2 = 1.0 - sse / sst if sst > 0 else 0.0
    return Fit(center=math.exp(-a / b), sigma=-1.0 / b, points=n, r2=r2)


def market_prob(m: dict[str, Any], center: float, sigma: float) -> float | None:
    """P(YES) for one threshold market under the lognormal. None for shapes we do not price."""
    kind = m.get("strike_type")
    lo, hi = m.get("floor_strike"), m.get("cap_strike")
    if kind in {"greater", "greater_or_equal"} and lo is not None:
        return prob_above(center, float(lo), sigma)
    if kind in {"less", "less_or_equal"}:
        k = hi if hi is not None else lo
        return None if k is None else 1.0 - prob_above(center, float(k), sigma)
    if kind == "between" and lo is not None and hi is not None:
        return max(0.0, prob_above(center, float(lo), sigma) - prob_above(center, float(hi), sigma))
    return None


# ---------------------------------------------------------------- pricing one event


@dataclass(frozen=True)
class Pricing:
    center: float
    spot: float
    basis: float  # ln(center / spot)
    sigma: float  # the width we price with (realized volatility, scaled)
    sigma_market: float  # the width the ladder implies
    points: int
    r2: float

    def note(self, p: float, hours: float) -> str:
        return (
            f"spot {self.spot:.4g}, ladder center {self.center:.4g} (basis {self.basis:+.2%}), "
            f"{hours:.1f}h left, width realized {self.sigma:.2%} vs ladder {self.sigma_market:.2%} "
            f"({self.points} strikes, r2 {self.r2:.2f}) -> P(yes) {p:.3f}"
        )


def price_event(
    markets: list[dict[str, Any]], symbol: str, feed: Feed, s: Settings,
    now: datetime | None = None, hours: float | None = None,
) -> Pricing:
    """Center from the ladder, width from realized volatility. Raises CommodityError if unusable."""
    now = now or datetime.now(UTC)
    series = feed.series(symbol)
    age = now.timestamp() - series.last_ts
    if age > STALE_SECONDS:
        raise CommodityError(f"{symbol} feed is {age / 60:.0f} min old (market closed?)")
    fit = fit_ladder(markets)
    if fit is None:
        raise CommodityError("ladder too thin to locate the center")
    if fit.r2 < 0.9:
        raise CommodityError(f"ladder does not fit a lognormal (r2 {fit.r2:.2f})")
    basis = math.log(fit.center / series.last)
    if abs(basis) > s.commodity_max_basis:
        raise CommodityError(
            f"ladder center {fit.center:.4g} is {basis:+.1%} from {symbol} spot {series.last:.4g}; "
            "different contract than the feed"
        )
    if hours is None:
        hours = _hours_to_close(markets, now)
    sigma = realized_sigma(series, hours) * s.commodity_vol_mult
    return Pricing(fit.center, series.last, basis, sigma, fit.sigma, fit.points, fit.r2)


def _hours_to_close(markets: list[dict[str, Any]], now: datetime) -> float:
    for m in markets:
        try:
            t = datetime.fromisoformat(m["close_time"])
        except (KeyError, ValueError, AttributeError):
            continue
        return (t - now).total_seconds() / 3600.0
    raise CommodityError("no close time")


class CommodityPricer:
    """Rates commodity markets. Holds only the price feed, so one instance serves every client."""

    def __init__(self, feed: Feed | None = None):
        self.feed: Feed = feed or YahooFeed()

    def price_markets(
        self, markets: list[dict[str, Any]], s: Settings, now: datetime | None = None
    ) -> dict[str, tuple[float, str]]:
        """ticker -> (P(yes), note) for one event's markets. Raises CommodityError."""
        now = now or datetime.now(UTC)
        if not markets:
            raise CommodityError("no markets")
        series = markets[0].get("series_ticker") or str(markets[0].get("event_ticker", "")).split("-")[0]
        symbol = symbol_for(series, load_instruments(s.data_dir))
        if symbol is None:
            raise CommodityError(f"{series} is not a supported commodity series")
        hours = _hours_to_close(markets, now)
        pricing = price_event(markets, symbol, self.feed, s, now, hours)
        out: dict[str, tuple[float, str]] = {}
        for m in markets:
            p = market_prob(m, pricing.center, pricing.sigma)
            if p is not None:
                out[m["ticker"]] = (min(max(p, 0.001), 0.999), pricing.note(p, hours))
        return out

    def estimate(self, client: Any, shot: Any, s: Settings) -> tuple[float, str]:
        """Fresh P(yes) for a shortlisted market, re-reading its whole ladder."""
        markets = client.event_markets(shot.event_ticker)
        for m in markets:
            m.setdefault("series_ticker", shot.series)
        priced = self.price_markets(markets, s)
        if shot.ticker not in priced:
            raise CommodityError(f"{shot.ticker} has no price under this model")
        return priced[shot.ticker]
