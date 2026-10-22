"""Your Kalshi account history, kept locally: every fill and settlement, bot or by hand.

The bot only ever knew its own orders. This pulls everything Kalshi has for the account (read-only) so
trades made in the app, including cash-outs, can be analysed next to the bot's. It is safe to re-run: rows
are keyed by Kalshi's own ids, and each run only asks for what is new (with an hour of overlap).
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from .kalshi import KalshiClient, KalshiError
from .store import Store

OVERLAP = 3600  # seconds re-read on every sync, in case Kalshi back-fills a late row


def _f(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _ts(iso: str | None) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else 0.0
    except ValueError:
        return 0.0


def fill_row(f: dict[str, Any]) -> tuple | None:
    ticker = f.get("market_ticker") or f.get("ticker")
    count, yes_price = _f(f.get("count_fp")), _f(f.get("yes_price_dollars"))
    if not (f.get("fill_id") and ticker and count and yes_price is not None and f.get("book_side") in {"bid", "ask"}):
        return None
    return (f["fill_id"], float(f.get("ts") or _ts(f.get("created_time"))), ticker, f["book_side"], count,
            yes_price, _f(f.get("no_price_dollars")), _f(f.get("fee_cost")) or 0.0, int(bool(f.get("is_taker"))),
            f.get("order_id"), f.get("action"), f.get("side"))


def settlement_row(s: dict[str, Any]) -> tuple | None:
    if not s.get("ticker") or not s.get("settled_time"):
        return None
    return (s["ticker"], _ts(s["settled_time"]), s.get("market_result"), _f(s.get("yes_count_fp")) or 0.0,
            _f(s.get("no_count_fp")) or 0.0, _f(s.get("yes_total_cost_dollars")) or 0.0,
            _f(s.get("no_total_cost_dollars")) or 0.0, _f(s.get("fee_cost")) or 0.0)


def sync(client: KalshiClient, store: Store, full: bool = False, market_budget: int = 40) -> dict[str, int]:
    """Pull new fills and settlements, and the open/close time of any market we have not looked up.

    Returns counts. market_budget caps market lookups per run so a first sync stays within the rate limit;
    the rest follow on the next run.
    """
    db = store.db
    last_fill = db.execute("SELECT MAX(ts) FROM fills").fetchone()[0]
    last_set = db.execute("SELECT MAX(settled_ts) FROM settlements").fetchone()[0]
    since_f = None if full or last_fill is None else int(last_fill) - OVERLAP
    since_s = None if full or last_set is None else int(last_set) - OVERLAP
    new_fills = new_sets = 0
    for f in client.portfolio_rows("/portfolio/fills", "fills", since_f):
        row = fill_row(f)
        if row is not None:
            cur = db.execute("INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
            new_fills += cur.rowcount
    for s in client.portfolio_rows("/portfolio/settlements", "settlements", since_s):
        row = settlement_row(s)
        if row is not None:
            cur = db.execute("INSERT OR IGNORE INTO settlements VALUES (?,?,?,?,?,?,?,?)", row)
            new_sets += cur.rowcount
    db.commit()
    looked = 0
    missing = [r[0] for r in db.execute(
        """SELECT DISTINCT ticker FROM (SELECT ticker FROM fills UNION SELECT ticker FROM settlements) seen
           WHERE ticker NOT IN (SELECT ticker FROM market_info)""")]
    for ticker in missing[:market_budget]:
        try:
            m = client.market(ticker)
        except KalshiError:
            continue
        db.execute("INSERT OR REPLACE INTO market_info VALUES (?,?,?,?,?,?)", (
            ticker, ticker.split("-")[0], _ts(m.get("open_time")), _ts(m.get("close_time")),
            m.get("result") or None, _f(m.get("floor_strike")),
        ))
        looked += 1
    db.commit()
    return {"fills": new_fills, "settlements": new_sets, "markets": looked, "markets_left": max(0, len(missing) - looked)}


def last_sync_age(store: Store) -> float | None:
    row = store.db.execute("SELECT MAX(ts) FROM fills").fetchone()[0]
    return None if row is None else time.time() - row
