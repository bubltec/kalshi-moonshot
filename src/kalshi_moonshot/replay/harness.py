"""Run a Tape through a real ProfitWatcher and report what it did.

The quotes and index ticks go in as socket events in time order; every sale signal is handed to the watcher's own `_sell`, which paper-sells into a
throw-away store. The watcher's clock is the tape's, so back-offs and look-backs behave as they did live.
"""

from __future__ import annotations

import heapq
import logging
import os
import tempfile
import time as real_time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .. import config, watcher
from ..store import Store
from ..strategy import fee_per_contract
from .tape import Tape, close_time_iso


@dataclass
class Outcome:
    tape: str
    signals: int  # sale signals the watcher raised
    sold: bool
    exit_price: float | None
    exit_left: float | None  # seconds before the close of the sale
    why: str
    sold_pnl: float  # the sale after its fee, or the hold result when nothing sold
    hold_pnl: float
    delta: float  # sold_pnl - hold_pnl: what the exits earned over holding to the close

    @property
    def stuck(self) -> bool:
        """The watcher raised a sale signal and nothing sold: a veto or a failed order held the position."""
        return self.signals > 0 and not self.sold


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Client:
    """Serves the quote the tape had at the clock's time, the way the REST market read would."""

    def __init__(self, tape: Tape, clock: _Clock) -> None:
        self.tape, self.clock = tape, clock
        self.at = 0

    def market(self, ticker: str) -> dict[str, Any]:
        q = self.tape.quotes
        while self.at + 1 < len(q) and q[self.at + 1][0] <= self.clock.now:
            self.at += 1
        bid, ask = (q[self.at][1], q[self.at][2]) if q else (0.0, 0.0)
        return {"ticker": ticker, "floor_strike": self.tape.ref, "close_time": close_time_iso(self.tape.close_ts), "yes_bid_dollars": str(bid), "yes_ask_dollars": str(ask), "title": self.tape.name}


@contextmanager
def _env(data_dir: Path):
    saved = {k: os.environ.get(k) for k in ("DATA_DIR", "DATABASE_URL")}
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ.pop("DATABASE_URL", None)  # a replay is always a private SQLite file, never the bot's database
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run(tape: Tape, lanes_toml: str, data_dir: Path | None = None) -> Outcome:
    """Replay `tape` under the lane definitions in `lanes_toml` (the text of a strategies.toml)."""
    with tempfile.TemporaryDirectory() as tmp, _env(data_dir or Path(tmp)):
        d = Path(os.environ["DATA_DIR"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "strategies.toml").write_text(lanes_toml)
        return _run(tape)


def _run(tape: Tape) -> Outcome:
    s = config.load()
    store = Store(s.db_path)
    store.db.execute(
        "INSERT INTO decisions (ts, ticker, action, mode, ask, side, lane, contracts, stake_usd, close_time) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (tape.bought_ts or tape.close_ts - 900.0, tape.ticker, "bet", "observe", tape.entry, tape.side, tape.lane, tape.contracts, tape.stake_usd, close_time_iso(tape.close_ts)),
    )
    store.db.commit()
    clock = _Clock()
    clock.now = tape.bought_ts or tape.close_ts - 900.0
    logging.disable(logging.INFO)  # the watcher logs every arming and shock watch; a sweep runs thousands
    shim = SimpleNamespace(time=clock, monotonic=clock, perf_counter=clock, sleep=lambda _s: None, strftime=real_time.strftime, gmtime=real_time.gmtime)
    real = watcher.time
    watcher.time = shim  # type: ignore[assignment]
    try:
        w = watcher.ProfitWatcher(lambda: s, _Client(tape, clock), lambda: Store(s.db_path))  # type: ignore[arg-type]
        w._shock_refs[(tape.ticker, tape.index_id)] = tape.ref
        if tape.variety:
            w._varieties[(tape.ticker, tape.lane)] = tape.variety
        w.refresh()
        signals = 0
        exit_at = 0.0
        events = heapq.merge(
            ((ts, 0, {"kind": "quote", "ticker": tape.ticker, "bid": bid, "ask": ask, "ts": ts}) for ts, bid, ask in tape.quotes),
            ((ts, 1, {"kind": "index", "index_id": tape.index_id, "value": v, "ts": ts}) for ts, v in tape.ticks),
            ((ts + 0.5, 2, {"kind": "trade", "ticker": tape.ticker, "taker": taker, "count": n, "price": px, "ts": ts + 0.5})  # a second's volume, placed mid-second
             for ts, yes_n, no_n, px in tape.flow for taker, n in (("yes", yes_n), ("no", no_n)) if n > 0),
            key=lambda x: (x[0], x[1]),
        )
        for ts, _order, e in events:
            if ts < clock.now - 1e-9 and ts < (tape.bought_ts or 0.0):
                continue  # before the buy: ticks only seed the look-back, which on_event does from the first one
            clock.now = max(clock.now, ts)
            w.on_event(e)
            while not w._jobs.empty():
                sig = w._jobs.get_nowait()
                signals += 1
                w._sell(sig.ticker, sig.lane, sig.why)
            if not store.open_bets():
                exit_at = clock.now
                break
    finally:
        watcher.time = real
    row = store.db.execute("SELECT exit_price, exit_reason FROM decisions WHERE ticker=? AND lane=? AND action='bet'", (tape.ticker, tape.lane)).fetchone()
    hold = tape.hold_pnl()
    if row is None or row["exit_price"] is None:
        return Outcome(tape.name, signals, False, None, None, "", hold, hold, 0.0)
    px = float(row["exit_price"])
    sold = tape.contracts * (px - tape.entry) - tape.contracts * fee_per_contract(px)
    return Outcome(tape.name, signals, True, px, round(tape.close_ts - exit_at, 1), str(row["exit_reason"] or ""), sold, hold, sold - hold)
