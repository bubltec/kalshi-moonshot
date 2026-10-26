"""The cheap-side buy, as a paper log: in the last 150 seconds of every 15-minute crypto market (held or not), the first second a side's exact settlement chance beats its ask by a bar.

It is the other half of the last-minute guard: the guard (watcher.py) sells a held favorite when the exact chance turns against it; this looks at every market and notes the side the exact chance says is underpriced, which is usually the cheap side of a market the crowd
has pushed too far. The chance is the same one (settlement.py): the 60-second average Kalshi settles on, computed from the live index. A replay on four days of tape (scripts/settlement_edge_stress.py) bought the first second at a 10-point bar: +43% a trade (+-20, clustered by slot),
positive in both halves of the slots and in every coin, but concentrated in tickets under 15c and so high-variance; this logs it live so a real record can confirm or kill it.
For each market it logs at most three paper bets, the first second the edge reaches 5, 10 and 15 points, each in the lane `settlement_edge` with the model's chance, the price and the edge saved, so the bar that pays is read from the results. Never an order.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from . import settlement
from .kalshiws import index_for_series
from .store import Store

log = logging.getLogger("moonshot")

LANE = "settlement_edge"
BARS = (0.05, 0.10, 0.15)  # the edge (the model's chance minus the ask) at which a paper bet is logged, once each per market
WINDOW = (20.0, 150.0)  # seconds before the close in which it looks
QUOTE_FRESH = 5.0


class SettlementEdge:
    def __init__(self, get_settings: Callable[[], Any], client: Any, store_factory: Callable[[], Store], tape_factory: Callable[[], Any]):
        self.get_settings = get_settings
        self.client = client
        self.store_factory = store_factory
        self.model = settlement.Model(tape_factory)
        self._quotes: dict[str, tuple[float, float, float]] = {}  # ticker -> (time, YES bid, YES ask)
        self._info: dict[str, tuple[float, float, str] | None] = {}  # ticker -> (strike, close time, settlement index)
        self._done: set[tuple[str, float]] = set()  # (ticker, bar) already logged
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def on_event(self, e: dict[str, Any]) -> None:
        """Called by the tape recorder for every event, on its thread: only note the latest index tick and quote, never compute."""
        if e.get("kind") == "index" and e.get("index_id") and e.get("value"):
            self.model.note_spot(str(e["index_id"]), float(e["ts"]), float(e["value"]))
        elif e.get("kind") == "quote" and e.get("bid") is not None and e.get("ask") is not None and str(e.get("ticker", "")).split("-")[0].endswith("15M"):
            self._quotes[e["ticker"]] = (float(e["ts"]), float(e["bid"]), float(e["ask"]))

    def _market(self, ticker: str) -> tuple[float, float, str] | None:
        if ticker not in self._info:
            info = None
            index_id = index_for_series(ticker.split("-")[0])
            try:
                m = self.client.market(ticker)
                strike = float(m.get("floor_strike") or 0.0)
                close = datetime.fromisoformat(str(m.get("close_time"))).timestamp()
                if index_id and strike > 0:
                    info = (strike, close, index_id)
            except Exception:
                info = None
            self._info[ticker] = info
        return self._info[ticker]

    def evaluate(self, now: float | None = None) -> int:
        """One pass over the markets with a recent quote. Returns how many paper bets were logged."""
        now = time.time() if now is None else now
        logged = 0
        for ticker, (qts, bid, ask) in list(self._quotes.items()):
            if now - qts > QUOTE_FRESH:
                continue
            info = self._market(ticker)
            if info is None:
                continue
            strike, close, index_id = info
            left = close - now
            if not (WINDOW[0] <= left <= WINDOW[1]):
                continue
            p_yes = self.model.chance_yes(index_id, strike, close, now)
            if p_yes is None:
                continue
            e_yes, e_no = p_yes - ask, (1.0 - p_yes) - (1.0 - bid)
            side, edge, price, p = ("yes", e_yes, ask, p_yes) if e_yes >= e_no else ("no", e_no, 1.0 - bid, 1.0 - p_yes)
            for bar in BARS:
                if edge >= bar and (ticker, bar) not in self._done and 0.01 < price < 0.99:
                    self._done.add((ticker, bar))
                    self._log(ticker, side, price, p, edge, bar, left, close, strike)
                    logged += 1
        closed = [tk for tk, i in self._info.items() if i is not None and i[1] < now - 120]  # markets that have closed no longer need their marks or quotes
        for tk in closed:
            self._info.pop(tk, None)
            self._quotes.pop(tk, None)
        if closed:
            self._done = {k for k in self._done if k[0] not in closed}
        return logged

    def _log(self, ticker: str, side: str, price: float, p: float, edge: float, bar: float, left: float, close: float, strike: float) -> None:
        try:
            self.store_factory().add_decision(
                ticker=ticker, title="", bid=0.0, ask=round(price, 4), model_p=round(p, 4), edge=round(edge, 4), action="bet", mode="observe", lane=LANE, category="Crypto", side=side, contracts=1.0, stake_usd=round(price, 4),
                reason=f"settlement edge: the exact chance {p:.2f} against a price of {price:.2f} ({100 * edge:.0f} points) with {left:.0f} s left", close_time=datetime.fromtimestamp(close).astimezone().isoformat(),
                context=json.dumps({"bar": bar, "edge": round(edge, 4), "left": round(left, 1), "strike": strike}))
        except Exception:
            log.debug("settlement edge: could not log %s", ticker, exc_info=True)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="settlement-edge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.evaluate()
            except Exception:
                log.exception("settlement edge pass failed")
