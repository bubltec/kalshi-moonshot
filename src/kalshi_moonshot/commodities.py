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

import logging
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

log = logging.getLogger("moonshot")
_N = NormalDist()


@dataclass(frozen=True)
class Instrument:
    prefix: str
    symbol: str
    suffixes: tuple[str, ...]
    realtime: bool = False  # the feed's price is current, so spot can anchor a market with no ladder
    proxy: bool = False  # feed tracks the asset with an offset (a gold token): measure it from ladders
    live: bool = True  # False = paper only: a lane that is live still only paper-trades this series


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
        Instrument(
            row["prefix"], row["symbol"], tuple(row.get("suffixes", [""])), bool(row.get("realtime", False)), bool(row.get("proxy", False)),
            bool(row.get("live", True)),
        )
        for row in raw.get("instrument", [])
    )


@lru_cache(maxsize=4)
def _patterns(
    instruments: tuple[Instrument, ...],
) -> tuple[tuple[re.Pattern[str], Instrument], ...]:
    return tuple(
        (re.compile(rf"^{re.escape(i.prefix)}({'|'.join(map(re.escape, i.suffixes))})$"), i)
        for i in instruments
    )


def instrument_for(
    series: str, instruments: tuple[Instrument, ...] | None = None
) -> Instrument | None:
    for pattern, inst in _patterns(instruments or load_instruments()):
        if pattern.match(series or ""):
            return inst
    return None


def symbol_for(series: str, instruments: tuple[Instrument, ...] | None = None) -> str | None:
    """Feed symbol for a Kalshi series, or None when the series is not a supported instrument."""
    inst = instrument_for(series, instruments)
    return inst.symbol if inst else None


BAR_SECONDS = 300  # feed bar size
SESSION_GAP = 15 * 60  # bigger gap between bars = market closed, not a price move
REALTIME_STALE_SECONDS = 120.0  # a real-time feed this old has dropped; do not trade on it
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
    sigma: float  # total width we price with: realized volatility plus feed-age and basis uncertainty
    sigma_market: float | None  # the width the ladder implies (None when the ladder was too thin)
    points: int
    r2: float
    age_s: float  # how old the feed's last price is
    anchor: str  # "ladder", "remembered" or "spot": where the center came from

    @property
    def remembered(self) -> bool:
        return self.anchor != "ladder"

    def note(self, p: float, hours: float) -> str:
        how = {
            "ladder": f"{self.points}-strike ladder fit",
            "remembered": "remembered basis",
            "spot": "spot (real-time feed)",
            "window": f"offset learned from {self.points} settled windows",
        }[self.anchor]
        ladder = "n/a" if self.sigma_market is None else f"{self.sigma_market:.2%}"
        return (
            f"spot {self.spot:.6g} ({self.age_s:.0f}s old), center {self.center:.6g} via {how} "
            f"(basis {self.basis:+.2%}), {hours:.2f}h left, width {self.sigma:.2%} vs ladder "
            f"{ladder} -> P(yes) {p:.3f}"
        )


WINDOW_POINTS = 6  # boundaries needed before a learned offset is trusted
WINDOW_MAX_AGE = 3 * 3600.0
WINDOW_REFRESH = 600.0
WINDOW_MINUTES = 15


@dataclass(frozen=True)
class WindowBasis:
    basis: float  # median of ln(Kalshi reference / feed price) at recent 15-minute boundaries
    sd: float  # robust spread of those offsets
    n: int
    ts: float


def window_basis(
    refs: dict[int, float], minutes: dict[int, float], now_ts: float
) -> WindowBasis | None:
    """Offset between Kalshi's reference and a feed, measured at 15-minute boundaries.

    refs maps a boundary (unix seconds) to the reference Kalshi published for it: a window's
    strike is the reference at its open and its settlement value is the reference at its close, so
    every boundary is known exactly. minutes maps each 1-minute candle's open time to its close.
    The reference at T is matched with the candle that closes at T (opened at T - 60 s).
    """
    import statistics

    offsets = [
        math.log(ref / minutes[t - 60]) for t, ref in refs.items()
        if ref > 0 and minutes.get(t - 60, 0.0) > 0
    ]
    if len(offsets) < WINDOW_POINTS:
        return None
    med = statistics.median(offsets)
    mad = statistics.median(abs(o - med) for o in offsets)
    return WindowBasis(med, 1.4826 * mad, len(offsets), now_ts)


def boundary_refs(markets: list[dict[str, Any]]) -> dict[int, float]:
    refs: dict[int, float] = {}
    for m in markets:
        try:
            close = int(datetime.fromisoformat(m["close_time"]).timestamp())
        except (KeyError, ValueError, TypeError):
            continue
        if m.get("floor_strike") is not None:
            refs[close - WINDOW_MINUTES * 60] = float(m["floor_strike"])
        try:
            if m.get("expiration_value") not in (None, ""):
                refs[close] = float(m["expiration_value"])
        except (TypeError, ValueError):
            pass
    return refs


# series -> (ln(center / spot), when seen, feed age in hours when seen). Keyed by series because one prefix can settle on different references
# (a daily settlement price vs a 15-minute index): a basis only transfers within the same series.
BasisMemory = dict[str, tuple[float, float, float]]


def price_event(
    markets: list[dict[str, Any]], inst: Instrument, feed: Feed, s: Settings,
    now: datetime | None = None, hours: float | None = None, memory: BasisMemory | None = None,
    windows: dict[str, WindowBasis] | None = None,
) -> Pricing:
    """Where the price is centered and how wide it can still move. Raises CommodityError.

    Center, in order of preference:
      ladder    a probit fit over this event's own liquid strikes
      spot      a real-time feed of the asset itself (crypto): the feed price, with a few basis
                points of width. This is what prices a 15-minute "up or down" market, which has no ladder.
      remembered  the basis (ln of ladder center over feed price) from a recent fit of the same
                  series, for feeds that are delayed or only track the asset (the gold token);
                  a real-time proxy may also borrow it from the same family
    Width: realized volatility over the time left plus the age of the feed's last price (a stale
    price hides a move), plus the uncertainty of the center.
    """
    now = now or datetime.now(UTC)
    memory = memory if memory is not None else {}
    series_t = markets[0].get("series_ticker", "") if markets else ""
    feed_series = feed.series(inst.symbol)
    age = max(0.0, now.timestamp() - feed_series.last_ts)
    limit_s = REALTIME_STALE_SECONDS if inst.realtime else s.commodity_max_stale_minutes * 60
    if age > limit_s:
        raise CommodityError(f"{inst.symbol} feed is {age / 60:.1f} min old (market closed?)")
    spot = feed_series.last
    fit = fit_ladder(markets)
    anchor = "ladder"
    unc = 0.0003
    sigma_mkt: float | None = None
    points, r2 = 0, 0.0
    if fit is not None and fit.r2 >= 0.9:
        center, points, r2, sigma_mkt = fit.center, fit.points, fit.r2, fit.sigma
        basis = math.log(center / spot)
        if abs(basis) > s.commodity_max_basis:
            raise CommodityError(
                f"ladder center {center:.6g} is {basis:+.1%} from {inst.symbol} spot {spot:.6g}; "
                "different contract than the feed"
            )
        memory[series_t] = (basis, now.timestamp(), age / 3600.0)
    else:
        limit = s.commodity_basis_age_hours * 3600
        learned = (windows or {}).get(series_t)
        seen, mult = None, 1.0
        if not (inst.realtime and not inst.proxy):  # a direct real-time price needs no offset
            seen = memory.get(series_t)
        if learned is not None and now.timestamp() - learned.ts <= WINDOW_MAX_AGE:
            # A 15-minute series: its offset from the feed was measured at the last boundaries.
            anchor, basis, points = "window", learned.basis, learned.n
            center = spot * math.exp(basis)
            unc = max(learned.sd, s.commodity_window_unc_floor)
        elif seen is not None and now.timestamp() - seen[1] <= limit:
            anchor, basis = "remembered", seen[0]
            center = spot * math.exp(basis)
            base = s.commodity_basis_unc
            if inst.realtime:
                base = s.commodity_proxy_unc if inst.proxy else s.commodity_realtime_unc
            unc = base * mult
            if seen[2] > 0.0:
                # Measured against a feed already `seen[2]` hours old, so it also holds that much
                # unseen move. Count it as width, not as basis.
                unc = math.sqrt(unc * unc + realized_sigma(feed_series, seen[2]) ** 2)
        elif inst.realtime and not inst.proxy:
            anchor, basis, center = "spot", 0.0, spot
            unc = s.commodity_realtime_unc
        else:
            raise CommodityError("ladder too thin to locate the center and no recent basis for it")
    if hours is None:
        hours = _hours_to_close(markets, now)
    # A feed that is `age` seconds old has an unseen move of about that much extra time.
    sigma = realized_sigma(feed_series, max(hours, 0.0) + age / 3600.0) * s.commodity_vol_mult
    total = math.sqrt(sigma * sigma + unc * unc)
    return Pricing(center, spot, basis, total, sigma_mkt, points, r2, age, anchor)


def _hours_to_close(markets: list[dict[str, Any]], now: datetime) -> float:
    for m in markets:
        try:
            t = datetime.fromisoformat(m["close_time"])
        except (KeyError, ValueError, AttributeError):
            continue
        return (t - now).total_seconds() / 3600.0
    raise CommodityError("no close time")


class CommodityPricer:
    """Rates priced-instrument markets. Holds the price feed and the remembered basis."""

    def __init__(self, feed: Feed | None = None):
        if feed is None:
            from .krakenfeed import RoutingFeed

            feed = RoutingFeed.for_instruments()
        self.feed: Feed = feed
        self.memory: BasisMemory = {}
        self.windows: dict[str, WindowBasis] = {}
        self._tried: dict[str, float] = {}  # series -> when learn_windows last looked

    def price_markets(
        self, markets: list[dict[str, Any]], s: Settings, now: datetime | None = None
    ) -> dict[str, tuple[float, str]]:
        """ticker -> (P(yes), note) for one event's markets. Raises CommodityError."""
        now = now or datetime.now(UTC)
        if not markets:
            raise CommodityError("no markets")
        series = markets[0].get("series_ticker") or str(markets[0].get("event_ticker", "")).split("-")[0]
        inst = instrument_for(series, load_instruments(s.data_dir))
        if inst is None:
            raise CommodityError(f"{series} is not a supported instrument series")
        hours = _hours_to_close(markets, now)
        pricing = price_event(markets, inst, self.feed, s, now, hours, self.memory, self.windows)
        floor = s.commodity_tail_floor
        out: dict[str, tuple[float, str]] = {}
        for m in markets:
            p = market_prob(m, pricing.center, pricing.sigma)
            if p is not None:
                p = min(max(p, floor), 1.0 - floor)
                out[m["ticker"]] = (p, pricing.note(p, hours))
        return out

    def learn_windows(self, client: Any, s: Settings, now: datetime | None = None) -> int:
        """Measure each 15-minute series' feed offset from its recent settled windows.

        Returns how many series were (re)learned. A failure for one series leaves its old value.
        """
        now_ts = (now or datetime.now(UTC)).timestamp()
        minutes_of = getattr(self.feed, "minute_closes", None)
        if minutes_of is None:
            return 0
        learned = 0
        for inst in load_instruments(s.data_dir):
            if f"{WINDOW_MINUTES}M" not in inst.suffixes:
                continue
            series = f"{inst.prefix}{WINDOW_MINUTES}M"
            if now_ts - self._tried.get(series, 0.0) < WINDOW_REFRESH:
                continue  # looked recently, whether or not it learned anything
            self._tried[series] = now_ts
            try:
                refs = boundary_refs(client.settled_markets(series))
                wb = window_basis(refs, minutes_of(inst.symbol), now_ts)
            except Exception as exc:
                log.warning("window offset for %s: %s", series, exc)
                continue
            if wb is not None:
                self.windows[series] = wb
                learned += 1
        return learned

    def estimate(self, client: Any, shot: Any, s: Settings) -> tuple[float, str]:
        """Fresh P(yes) for a shortlisted market, re-reading its whole ladder."""
        markets = client.event_markets(shot.event_ticker)
        for m in markets:
            m.setdefault("series_ticker", shot.series)
        priced = self.price_markets(markets, s)
        if shot.ticker not in priced:
            raise CommodityError(f"{shot.ticker} has no price under this model")
        return priced[shot.ticker]
