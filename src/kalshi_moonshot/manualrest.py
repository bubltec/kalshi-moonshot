"""A resting sell on a position you opened by hand, priced from the curve and the clock, with a late guard.

User, 2026-11-11: "what if instead of trying to figure out when to sell, we just set sell orders and adjust them as time
runs out", then chose "resting sell at the curve's chance plus a margin that shrinks with time left", with the lanes'
time-to-go sale as the guard against the total loss. Study: scripts/manual_position_exits.py, results in
studies/results/2026-11-11-manual-position-exits.txt (100 hand-opened positions: +$219.18 against +$76.47 as traded,
13 losing 90% or more against 32, both halves about +$110; held made +$544.67, most of it one position).

The rule, for a hand-opened position on a 15-minute market whose curve can be read (variants.curve_edge_now):
  rest   an ask at the curve's chance that the side pays + far * sqrt(seconds left / 900), repriced every REPRICE_EVERY
         seconds. It rests first, so a fill is charged no taker fee. The margin is the whole of `far` at the open and
         nothing at the close.
  go     with `guard` seconds or less left, a position whose curve chance is under one half is sold at the bid.
A position with no curve reading (no index, a short history, a longer market) is left to the older hand-placed rules.

Orders are kept in memory, not in the store: the store's orders are the bot's own buys and sells, and a hand-opened
position must not change them. Every order carries a short expiry, so one left behind by a restart dies by itself.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from .kalshi import KalshiError

log = logging.getLogger("moonshot")

REPRICE_EVERY = 15.0  # seconds between reprices, as in the replay
ORDER_LIFE = 40.0  # an order's own expiry: long enough to cover a slow cycle, short enough that an orphan is gone within the minute
QUIET_LAST = 8.0  # no new order in the last seconds of the market, as in the replay
MIN_PRICE = 0.05
GO_PREFIX = "curve time-to-go"  # a sale reason starting with this may sell a hand-placed position under its cost (watcher._sell_manual)


@dataclass
class Working:
    order_id: str
    price: float
    count: float
    at: float


def plan(chance: float | None, left: float, bid: float, far: float, guard: float) -> tuple[str, float]:
    """What to do with one position now: ("go", 0) sell at the bid, ("rest", price) work an ask there, ("hold", 0) nothing.

    The ask is never placed at or under the bid: a post-only order there is refused. When the bid is already at the
    curve's price the ask goes one cent over the bid, so the sale stays free of the taker fee."""
    if chance is None or far <= 0 or left <= 0 or left > 900:
        return "hold", 0.0
    if guard > 0 and left <= guard and chance < 0.5 and bid > 0:
        return "go", 0.0
    if left < QUIET_LAST:
        return "hold", 0.0
    price = min(0.99, chance + far * math.sqrt(left / 900.0))
    price = math.ceil(round(price * 100, 6)) / 100.0  # a whole cent, never under the rule's price
    if bid > 0:
        price = max(price, round(bid + 0.01, 2))
    price = min(price, 0.99)
    if price < MIN_PRICE:
        return "hold", 0.0
    return "rest", price


class ManualRest:
    """The resting sells on hand-opened positions: one working order per ticker, cancelled and replaced as its price moves."""

    def __init__(self, client: Any):
        self.client = client
        self.working: dict[str, Working] = {}
        self.said: dict[str, float] = {}  # ticker -> the price last logged while trading is off

    def cancel(self, ticker: str) -> None:
        """Take this ticker's ask off the book (before another sale of the same contracts, or when the position is gone)."""
        w = self.working.pop(ticker, None)
        if w is None or w.at + ORDER_LIFE <= time.time():
            return
        try:
            self.client.cancel_order(w.order_id)
        except KalshiError as exc:
            log.debug("manual rest: cancel of %s on %s failed (%s); it expires by itself", w.order_id, ticker, exc)

    def forget_gone(self, held: set[str]) -> list[str]:
        """Positions that are no longer on the account: their order filled, or you sold, or the market closed. Returns the tickers whose ask was still working."""
        gone = [tk for tk in self.working if tk not in held]
        for tk in gone:
            w = self.working.pop(tk)
            log.info("manual rest: %s is no longer held; its ask at %.2f for %g filled or ended.", tk, w.price, w.count)
        for tk in [tk for tk in self.said if tk not in held]:
            self.said.pop(tk, None)
        return gone

    def step(self, ticker: str, side: str, count: float, action: str, price: float, left: float, live: bool, now: float | None = None) -> str:
        """Carry out plan()'s answer for one position. Returns a note for the log ('' = nothing changed)."""
        now = time.time() if now is None else now
        w = self.working.get(ticker)
        if action != "rest":
            if w is not None:
                self.cancel(ticker)
                return f"ask at {w.price:.2f} withdrawn"
            return ""
        alive = w is not None and w.at + ORDER_LIFE > now + 3.0
        if alive and now - w.at < REPRICE_EVERY:
            return ""  # the replay reprices every 15 s; between reprices the ask stands
        if alive and abs(w.price - price) < 0.005 and abs(w.count - count) < 0.005 and w.at + ORDER_LIFE > now + REPRICE_EVERY:
            return ""  # same price and still good for another cycle
        if not live:
            if abs(self.said.get(ticker, -1.0) - price) >= 0.005:
                self.said[ticker] = price
                return f"would rest an ask at {price:.2f} for {count:g} {side} (trading is off)"
            return ""
        if w is not None:
            self.cancel(ticker)
        life = min(ORDER_LIFE, left - 3.0)
        if life < 5.0:
            return ""
        try:
            resp = self.client.sell_resting(ticker, side, round(count, 2), price, life)
        except KalshiError as exc:
            return f"ask at {price:.2f} refused ({exc})"
        order_id = resp.get("order_id") or (resp.get("order") or {}).get("order_id") or ""
        if not order_id:
            return f"ask at {price:.2f} sent but Kalshi named no order"
        self.working[ticker] = Working(order_id, price, count, now)
        return f"ask rests at {price:.2f} for {count:g} {side}" + (f" (was {w.price:.2f})" if w is not None else "")
