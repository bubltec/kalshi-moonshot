"""The order journal and its dead-letter retry (2026-11-26, user, on BNB 09:15 YES bought 09:03:09 ET: filled 16.66 at 0.28, a push restarted the bot 26 s later, the bet's row never reached the book and nothing sold it: "we need to backfill this order"; "if things fail they need to go into DLQ so they don't block anything and we can retry on the side").

An order that is sent is written here, one appended line, BEFORE the buy waits on its fill. The bet's own row is written later (in memory first, behind to the database), so a restart, a crash or five failed writes between the order and the row used to leave a position on the account that no watcher knew. The journal is the durable intent that survives that. It is a file on the data volume, not a table: appending a line is not a database call, so the buy and the tick keep their 60 ms.

A background thread (`run`, started by the worker) is the dead-letter retry: each pass looks at every intent that has no bet row and is old enough not to belong to a live evaluation, asks Kalshi what the order did, and writes the bet row from the fills (`recover_once`). A pass that fails leaves the intent where it is for the next pass; nothing waits on it. Memory and files only on the buy side; the database and Kalshi are read here, in the background."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("moonshot")

FILE = "order_journal.jsonl"
STARTED = time.time()
MIN_AGE = 150.0  # an intent of THIS process younger than this belongs to its live evaluation (a resting buy waits up to about a minute, then the row is written)
MAX_AGE = 6 * 3600.0  # an intent older than this is given up (logged)
EVERY = 60.0
COMPACT_LINES = 400


def path(data_dir: Any) -> Path:
    return Path(data_dir) / FILE


def _append(data_dir: Any, row: dict[str, Any]) -> None:
    line = (json.dumps(row, separators=(",", ":")) + "\n").encode()
    p = path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)  # one write of one line: appended whole, even with two threads
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def intent(data_dir: Any, *, lane: str, shot: Any, order_id: str, count: float, price: float, kind: str) -> None:
    """Record an order that was just sent. Never raises and never waits on the database."""
    try:
        _append(data_dir, {
            "ts": time.time(), "order_id": order_id, "lane": lane, "ticker": shot.ticker, "side": shot.side, "count": count, "price": price, "kind": kind,
            "bid": float(shot.bid or 0.0), "ask": float(shot.ask or 0.0), "close_time": shot.close_time or "", "event_ticker": shot.event_ticker or "", "category": shot.category or "",
        })
    except Exception as exc:
        log.warning("order journal: could not record the order %s on %s (%r); a restart now would not find it", order_id, getattr(shot, "ticker", "?"), exc)


def settle(data_dir: Any, order_id: str, note: str) -> None:
    with contextlib.suppress(Exception):
        _append(data_dir, {"done": order_id, "ts": time.time(), "note": note})


def pending(data_dir: Any, now: float | None = None) -> list[dict[str, Any]]:
    """The intents with no `done` line, oldest first. A file with many finished lines is rewritten with only what is pending."""
    p = path(data_dir)
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return []
    intents: dict[str, dict[str, Any]] = {}
    done: set[str] = set()
    for ln in lines:
        try:
            row = json.loads(ln)
        except ValueError:
            continue
        if "done" in row:
            done.add(row["done"])
        elif row.get("order_id") is not None:
            intents[row["order_id"] or f"{row.get('ticker')}@{row.get('ts')}"] = row
    left = [r for k, r in intents.items() if k not in done]
    if len(lines) > COMPACT_LINES:
        with contextlib.suppress(OSError):
            tmp = p.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in left))
            os.replace(tmp, p)
    return sorted(left, key=lambda r: r["ts"])


def _has_bet(store: Any, e: dict[str, Any]) -> bool:
    row = store.db.execute(
        "SELECT 1 FROM decisions WHERE lane=? AND ticker=? AND side=? AND action='bet' AND ts>=? LIMIT 1", (e["lane"], e["ticker"], e["side"], e["ts"] - 5.0)
    ).fetchone()
    return row is not None


def _filled(client: Any, e: dict[str, Any]) -> tuple[float, float]:
    """(contracts, dollars including fee) the account's fills show for this market and side since the order was sent: every order of the buy, so an order the follow rule moved by cancel-and-place is counted too."""
    from . import account, memguard

    with memguard.allowed():
        rows = client.portfolio_rows("/portfolio/fills", "fills", int(e["ts"]) - 5, pages=1)
    filled = spent = fees = 0.0
    seen: set[str] = set()
    for f in rows:
        row = account.fill_row(f)
        if row is None or row[0] in seen or row[2] != e["ticker"] or row[11] != e["side"] or (row[10] or "buy") != "buy":
            continue
        seen.add(row[0])
        n, yes_price, fee = float(row[4]), float(row[5]), float(row[7] or 0.0)
        filled += n
        spent += (yes_price if e["side"] == "yes" else 1.0 - yes_price) * n + fee
        fees += fee
    return filled, spent, fees


def _recover(s: Any, client: Any, store: Any, e: dict[str, Any]) -> str:
    from .runner import _order_state

    if _has_bet(store, e):
        settle(s.data_dir, e["order_id"], "the bet row exists")
        return f"{e['ticker']}: recorded"
    if e["order_id"] and _order_state(client, e["ticker"], e["order_id"]) == "resting":
        with contextlib.suppress(Exception):
            client.cancel_order(e["order_id"])  # an order nobody is watching is not left to fill
        time.sleep(2.0)
    filled, spent, fees = _filled(client, e)
    if filled <= 0:
        settle(s.data_dir, e["order_id"], "nothing filled")
        return f"{e['ticker']}: nothing filled"
    paid = spent / filled
    # THE BUY'S ORDER ROW IS RECOVERED WITH THE BET (2026-11-27, SOL 00:45 YES: a resting buy was sent at 00:32:56, a push restarted the bot seconds later, the
    # bet's row was recovered from this journal, and the one rule's sale at 0.84 at 00:36:12 was booked ON PAPER, because a live sale sells what the orders
    # table says the account holds (store.net_contracts) and the recovery wrote no order row; the real 19.23 contracts went to zero, about -$15. User:
    # "another zero. This was a tough one it had several places it could have sold at"). Without this row every exit of a recovered bet is a paper one.
    if e["order_id"] and not store.db.execute("SELECT 1 FROM orders WHERE order_id=? AND COALESCE(side,'buy')='buy' LIMIT 1", (e["order_id"],)).fetchone():
        store.add_order(ticker=e["ticker"], env=s.env, count=e["count"], price=round((spent - fees) / filled, 4), fill_count=filled, cost_usd=round(spent, 4), fee_usd=round(fees, 4), order_id=e["order_id"], contract=e["side"])
    store.add_decision(
        ticker=e["ticker"], title="", bid=round(e["bid"], 4), ask=round(e["ask"], 4), model_p=round(e["ask"], 4), edge=0.0, action="bet", mode=s.mode,
        reason=f"bought {e['side'].upper()} {filled:g}/{e['count']:g} at about {paid:.2f}, fee included: RECOVERED from the order journal ({e['kind']} order {e['order_id'][:13]} sent {time.strftime('%H:%M:%S', time.gmtime(e['ts']))} UTC; the bot stopped or the write failed before the bet's own row was made). Its entry reasoning was not kept.",
        close_time=e["close_time"], lane=e["lane"], category=e["category"], event_ticker=e["event_ticker"], side=e["side"], contracts=filled, stake_usd=round(spent, 4),
    )
    settle(s.data_dir, e["order_id"], f"recovered {filled:g} contracts")
    return f"{e['ticker']}: recovered {filled:g} contracts for {e['lane']}"


def recover_once(s: Any, client: Any, store: Any, now: float | None = None) -> list[str]:
    """One pass over the intents without a bet row. Returns what it did, one line each. A failure is logged and the intent stays for the next pass (the dead-letter retry)."""
    now = now if now is not None else time.time()
    out: list[str] = []
    for e in pending(s.data_dir):
        age = now - e["ts"]
        if e["ts"] >= STARTED and age < MIN_AGE:
            continue  # this process's own, still inside its evaluation
        if age > MAX_AGE:
            settle(s.data_dir, e["order_id"], "given up: too old")
            log.error("order journal: gave up on %s %s (%s, sent %.0f min ago): check the account", e["lane"], e["ticker"], e["order_id"], age / 60)
            continue
        try:
            out.append(_recover(s, client, store, e))
        except Exception as exc:
            log.warning("order journal: %s %s not settled yet, retried in %.0f s: %r", e["lane"], e["ticker"], EVERY, exc)
    for line in out:
        log.warning("order journal: %s", line)
    return out


def run(get_settings: Callable[[], Any], client: Any, make_store: Callable[[], Any], stopping: Callable[[], bool]) -> None:
    """The retry thread: one pass at start (a restart is when orders are orphaned) and one a minute after. It has its own store."""
    store = make_store()
    while not stopping():
        try:
            recover_once(get_settings(), client, store)
        except Exception:
            log.exception("order journal: a pass failed")
            store = make_store()
        for _ in range(int(EVERY)):
            if stopping():
                return
            time.sleep(1.0)
