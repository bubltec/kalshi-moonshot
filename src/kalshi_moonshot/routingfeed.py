"""Routes a price symbol to the feed that owns it: Hyperliquid ("hl:"), Kalshi's own perp reference ("kp:") or the fallback. Was inside krakenfeed.py, which is gone."""

from __future__ import annotations

from typing import Any

from .commodities import CommodityError, Feed, Series


class RoutingFeed:
    """Sends "hl:" symbols to Hyperliquid, "kp:" to Kalshi's own perp reference on the tape, and everything else to the fallback feed. (Kraken was a fourth route until 2026-11-14: the user, "you can drop kraken and coinbase too".)"""

    def __init__(self, other: Feed | None = None, hyperliquid: Any = None, kalshi: Any = None):
        self._other = other
        self._hl = hyperliquid
        self._kp = kalshi

    @classmethod
    def for_instruments(cls) -> RoutingFeed:
        """Subscribe to every Hyperliquid symbol the packaged instruments use, up front: one connection."""
        from .commodities import load_instruments
        from .hlfeed import PREFIX as HL_PREFIX
        from .hlfeed import HyperliquidFeed

        names = [i.symbol for i in load_instruments()]
        return cls(hyperliquid=HyperliquidFeed([n for n in names if n.startswith(HL_PREFIX)]))

    @property
    def kalshi(self) -> Any:
        if self._kp is None:
            from .kalshifeed import KalshiPerpFeed

            self._kp = KalshiPerpFeed()
        return self._kp

    @property
    def hyperliquid(self) -> Any:
        if self._hl is None:
            from .hlfeed import HyperliquidFeed

            self._hl = HyperliquidFeed()
        return self._hl

    def minute_closes(self, symbol: str, hours: float = 4.0) -> dict[int, float]:
        """1-minute closes (keyed by open time) from whichever feed owns the symbol."""
        if symbol.startswith("hl:"):
            return self.hyperliquid.minute_closes(symbol, hours)
        if symbol.startswith("kp:"):
            return self.kalshi.minute_closes(symbol, hours)
        raise CommodityError(f"no minute history for {symbol}")

    def ticks(self, symbol: str, hours: float) -> tuple[list[float], list[float]]:
        """Raw recorded readings, for the feeds that keep them (the Kalshi perp reference)."""
        if symbol.startswith("kp:"):
            return self.kalshi.ticks(symbol, hours)
        raise CommodityError(f"no tick history for {symbol}")

    @property
    def other(self) -> Feed:
        if self._other is None:
            from .commodities import YahooFeed

            self._other = YahooFeed()
        return self._other

    def series(self, symbol: str) -> Series:
        if symbol.startswith("hl:"):
            return self.hyperliquid.series(symbol)
        if symbol.startswith("kp:"):
            return self.kalshi.series(symbol)
        return self.other.series(symbol)
