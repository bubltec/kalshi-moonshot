"""Positions you opened by hand, for the watcher to protect.

The bot's own bets carry their lane's exit rules. A position you bought in the app has none, and a cash-out only works when
someone is watching. With MANUAL_EXIT on, the watcher treats every open position on the account that the bot did not open
as a lane of its own ("manual"): once the bid is MANUAL_EXIT_ACTIVATE over the price paid, it sells on a peak-crash (a drop
of MANUAL_EXIT_DROP off a peak that held a few seconds, or MANUAL_EXIT_FAST at once). It only ever takes a profit: it
never sells a hand-placed position for less than MANUAL_EXIT_MIN_PROFIT over what it cost (after the fee) to take a profit. MANUAL_EXIT_STOP_LOSS adds the bot's own stop-loss, on the same hours rules. MANUAL_EXIT_SKIP names the ticker
prefixes to leave alone (a position you mean to hold for days).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .kalshi import KalshiClient, KalshiError
from .store import Store

log = logging.getLogger("moonshot")

LANE = "manual"
POLL_EVERY = 10.0  # seconds between reads of the account's positions


@dataclass(frozen=True)
class Position:
    ticker: str
    side: str  # "yes" or "no"
    count: float
    entry: float  # average price paid per contract, in the side's own terms


def parse_positions(body: dict[str, Any]) -> list[Position]:
    """Open positions from /portfolio/positions. A positive count is YES, a negative one NO."""
    out: list[Position] = []
    for m in body.get("market_positions", []):
        try:
            qty = float(m.get("position_fp") or 0.0)
            exposure = float(m.get("market_exposure_dollars") or 0.0)
        except (TypeError, ValueError):
            continue
        if abs(qty) < 0.01 or exposure <= 0 or not m.get("ticker"):
            continue
        out.append(Position(m["ticker"], "yes" if qty > 0 else "no", abs(qty), exposure / abs(qty)))
    return out


def skipped(ticker: str, s: Settings) -> bool:
    return any(ticker.startswith(p) for p in s.manual_exit_skip)


class ManualPositions:
    """The account's hand-placed open positions, read at most every POLL_EVERY seconds."""

    def __init__(self, client: KalshiClient):
        self.client = client
        self._at = 0.0
        self._cache: list[Position] = []
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        """The next read goes to the exchange (a fill just arrived on the socket)."""
        with self._lock:
            self._at = 0.0

    def positions(self, s: Settings, store: Store, fresh: bool = False) -> list[Position]:
        with self._lock:
            if not fresh and time.time() - self._at < POLL_EVERY:
                return list(self._cache)
            try:
                every = parse_positions(self.client.positions())
            except KalshiError as exc:
                log.debug("manual: could not read positions (%s)", exc)
                return list(self._cache)
            # What the bot really bought is managed by its own lane; only the rest of a position is yours. A paper bet on the same market
            # is not a position on the account, so it does not count.
            mine: list[Position] = []
            for p in every:
                if skipped(p.ticker, s):
                    continue
                bot_n = store.net_contracts(p.ticker, s.env)
                if bot_n > 0.005:
                    left = p.count - bot_n
                    if left < 0.05:
                        continue  # all of it is the bot's
                    paid = p.entry * p.count - store.net_cost(p.ticker, s.env)
                    p = Position(p.ticker, p.side, left, paid / left if paid > 0 else p.entry)
                mine.append(p)
            self._cache = mine
            self._at = time.time()
            return list(self._cache)
