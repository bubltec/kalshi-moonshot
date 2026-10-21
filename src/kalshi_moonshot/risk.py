"""The one gate every order passes through. Nothing else in the codebase places orders.

Rules, in order: kill file, real-money guard, no duplicate position, no second side of a
mutually exclusive event, open-position cap, per-trade cap, daily loss limit. A refusal returns
a reason and the caller logs it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Settings
from .store import Store
from .trips import realized_since

EXCHANGE_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""


def start_of_day(now: float | None = None) -> float:
    """Midnight New York time, the exchange's day, as a unix time."""
    now = time.time() if now is None else now
    local = datetime.fromtimestamp(now, EXCHANGE_TZ)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def check(
    s: Settings,
    store: Store,
    ticker: str,
    cost_usd: float,
    now: float | None = None,
    max_trade: float | None = None,
    event_ticker: str = "",
    mutually_exclusive: bool = False,
) -> Verdict:
    """max_trade lets a lane use a tighter per-trade cap. It can never loosen the global one."""
    cap = s.max_usd_per_trade if max_trade is None else min(max_trade, s.max_usd_per_trade)
    if s.kill_file.exists():
        return Verdict(False, "kill switch file is present")
    if s.mode != "trade":
        return Verdict(False, "MODE is observe")
    if s.env == "prod" and not s.allow_real_money:
        return Verdict(False, "KALSHI_ENV=prod but ALLOW_REAL_MONEY is not true")
    if cost_usd <= 0:
        return Verdict(False, "nothing affordable at this price")
    if cost_usd > cap + 1e-9:
        return Verdict(False, f"cost ${cost_usd:.2f} over per-trade cap ${cap:.2f}")
    held = store.open_position_tickers(s.env)
    if ticker in held:
        return Verdict(False, "already holding this market")
    if mutually_exclusive:
        others = store.bet_tickers_in_event(event_ticker) - {ticker}
        if others:
            return Verdict(False, f"already on another side of this event ({min(others)})")
    if len(held) >= s.max_open_positions:
        return Verdict(False, f"{len(held)} open positions (cap {s.max_open_positions})")
    # Realized losses today, from the synced account history (so your own trades count too). It is as fresh as the
    # last account sync (ACCOUNT_SYNC_MINUTES). Wins offset losses; money merely spent on open positions does not count.
    lost = -realized_since(store, start_of_day(now))
    if s.max_loss_per_day > 0 and lost >= s.max_loss_per_day - 1e-9:
        return Verdict(False, f"daily loss limit: down ${lost:.2f} today (limit ${s.max_loss_per_day:.2f})")
    return Verdict(True)
