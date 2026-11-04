"""Kalshi's authenticated WebSocket: live contract quotes and the settlement indexes themselves.

Five channels on one connection (ticker and trade are separate subscriptions, so their markets can change without a reconnect):

  fill                every fill on the account, the bot's and yours from the app (the watcher enrolls a hand-placed buy from it)
  ticker              every change of a market's bid / ask / last price, as it happens
  trade               every public print, with the taker's side
  cfbenchmarks_value_5hz  the same indexes up to five times a second, with no averages (the strike-through sale reads it; the tape does not)
  cfbenchmarks_value  the CF Benchmarks indexes Kalshi settles its crypto markets on (BRTI for bitcoin,
                      ETHUSD_RTI, ...), about once a second, each with its trailing 60-second average,
                      which is the quantity the 15-minute crypto markets compare

It is read-only: the connection is signed like any API call, but nothing here can place an order.
Events are handed to a callback as plain dicts; the callback must be quick.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from .kalshi import KalshiClient

log = logging.getLogger("moonshot")

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

# Kalshi series prefix -> the CF Benchmarks index that settles it.
INDEX_FOR = {
    "KXBTC": "BRTI", "KXETH": "ETHUSD_RTI", "KXSOL": "SOLUSD_RTI", "KXXRP": "XRPUSD_RTI",
    "KXDOGE": "DOGEUSD_RTI",
    # 2026-11-04: recorded to study, not traded or scored (RECORD_ONLY). The ids were checked against the stream (40 ticks in 40 s each); TON has no index id there, so its quotes are recorded without a spot.
    "KXADA": "ADAUSD_RTI", "KXBCH": "BCHUSD_RTI", "KXBNB": "BNBUSD_RTI", "KXHYPE": "HYPEUSD_RTI", "KXNEAR": "NEARUSD_RTI", "KXZEC": "ZECUSD_RTI",
}
RECORD_ONLY = frozenset({"KXADA15M", "KXBCH15M", "KXBNB15M", "KXHYPE15M", "KXNEAR15M", "KXTON15M", "KXZEC15M"})  # crypto 15-minute series that are only recorded: the ledger, the gates and the lanes leave them out until each has been studied


def index_for_series(series: str) -> str | None:
    for prefix, index in INDEX_FOR.items():
        if series.startswith(prefix) and series[len(prefix):] in {"", "D", "15M"}:
            return index
    return None


def strike_index_for(series: str) -> str | None:
    """The index a 15-minute market's strike is read against: the CF Benchmarks index for crypto, the Pyth feed for commodities."""
    from .pythfeed import (
        index_for_series as pyth_index,  # late: pythfeed needs httpx only when it runs
    )

    return index_for_series(series) or pyth_index(series)


def _f(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_message(msg: dict[str, Any], received: float | None = None) -> dict[str, Any] | None:
    """A quote or an index tick as a flat dict; anything else (acks, errors, pings): None."""
    kind, body = msg.get("type"), msg.get("msg") or {}
    now = received if received is not None else time.time()
    if kind == "ticker":
        bid, ask = _f(body.get("yes_bid_dollars")), _f(body.get("yes_ask_dollars"))
        if not body.get("market_ticker") or bid is None or ask is None:
            return None
        return {"kind": "quote", "ts": now, "ticker": body["market_ticker"], "bid": bid, "ask": ask,
                "last": _f(body.get("price_dollars")), "sts": msg.get("sending_ts_ms"),
                "bid_size": _f(body.get("yes_bid_size_fp")), "ask_size": _f(body.get("yes_ask_size_fp"))}
    if kind == "trade":
        count, price = _f(body.get("count_fp")), _f(body.get("yes_price_dollars"))
        if not body.get("market_ticker") or not count or price is None:
            return None
        side = body.get("taker_outcome_side") or body.get("taker_side")  # the outcome the taker bought: "yes" or "no"
        if side not in {"yes", "no"}:
            return None
        return {"kind": "trade", "ts": now, "ticker": body["market_ticker"], "price": price, "count": count, "taker": side}
    if kind == "fill":  # a fill on this account, from the bot or from the app (private channel: needs the signed connection)
        count, price = _f(body.get("count_fp")), _f(body.get("yes_price_dollars"))
        side = body.get("purchased_side") or body.get("side")
        if not body.get("market_ticker") or not count or side not in {"yes", "no"}:
            return None
        return {"kind": "fill", "ts": now, "ticker": body["market_ticker"], "side": side, "action": body.get("action") or "",
                "count": count, "price": price, "taker": bool(body.get("is_taker")),
                "trade_id": body.get("trade_id") or "", "order_id": body.get("order_id") or ""}
    if kind == "cfbenchmarks_value":
        try:
            frame = json.loads(body.get("data") or "{}")
        except ValueError:
            return None
        value = _f(frame.get("value"))
        if value is None or not body.get("index_id"):
            return None
        avg = _f((body.get("avg_60s_data") or {}).get("value"))
        win = _f((body.get("last_60s_windowed_average_15min") or {}).get("value"))
        return {"kind": "index", "ts": now, "index_id": body["index_id"], "value": value,
                "avg60": avg, "quarter_avg": win, "src_ms": frame.get("time"), "sts": msg.get("sending_ts_ms")}
    if kind == "cfbenchmarks_value_5hz":  # up to five ticks a second, no averages: for the sale, never the tape
        value = _f(body.get("value_usd"))
        if value is None or not body.get("index_id"):
            return None
        return {"kind": "index_fast", "ts": now, "index_id": body["index_id"], "value": value,
                "src_ms": body.get("source_ts_ms"), "sts": msg.get("sending_ts_ms")}
    return None


class KalshiStream:
    """Keeps one connection open, reconnects with backoff, and resubscribes when the market set changes."""

    def __init__(self, client: KalshiClient, on_event: Callable[[dict[str, Any]], None],
                 indices: list[str] | None = None, index_feed: bool = True, fills: bool = True):
        """index_feed and fills are off for a stream that only records extra markets: the live stream alone carries the settlement indexes and the account's fills."""
        self.client = client
        self.on_event = on_event
        self.index_feed, self.fills = index_feed, fills
        self.indices = sorted(set(indices or INDEX_FOR.values()))
        self._tickers: set[str] = set()
        self._live: set[str] = set()  # the markets the open connection is subscribed to right now
        self._sids: dict[str, int] = {}  # channel -> subscription id on the open connection
        self._next_cmd = 100  # ids for update commands; the subscribe commands use 1 to 4
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self._stop = False
        self.connected_at: float | None = None
        self.last_event_at: float | None = None

    def set_tickers(self, tickers: set[str]) -> None:
        """Replace the markets whose quotes are streamed. An open connection adds and drops markets in place, so the index feed and fills keep flowing."""
        with self._lock:
            changed = tickers != self._tickers
            self._tickers = set(tickers)
        if changed and self._ws is not None and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._sync(), self._loop)

    async def _sync(self) -> None:
        """Bring the open connection's markets in line with the wanted set. Runs on the stream's own loop, so syncs never interleave."""
        ws = self._ws
        if ws is None:
            return
        try:
            with self._lock:
                want = set(self._tickers)
            if want == self._live:
                return
            sids = [self._sids.get("ticker"), self._sids.get("trade")]
            if not self._live:  # nothing subscribed yet (or all dropped): a fresh subscribe
                if self._sids:
                    return  # a subscribe is already in flight; its ack runs this again
                for message in self._market_subscriptions(sorted(want)):
                    await ws.send(json.dumps(message))
                self._live = want
                return
            if None in sids:
                return  # the acks have not arrived; each runs this again
            if not want:
                await ws.send(json.dumps({"id": self._cmd(), "cmd": "unsubscribe", "params": {"sids": sids}}))
                self._live, self._sids = set(), {}
                return
            added, removed = sorted(want - self._live), sorted(self._live - want)
            for channel, sid in zip(("ticker", "trade"), sids, strict=True):
                for action, tickers in (("add_markets", added), ("delete_markets", removed)):
                    if tickers:
                        params: dict[str, Any] = {"sids": [sid], "action": action, "market_tickers": tickers}
                        if channel == "ticker" and action == "add_markets":
                            params["send_initial_snapshot"] = True
                        await ws.send(json.dumps({"id": self._cmd(), "cmd": "update_subscription", "params": params}))
            self._live = want
        except Exception as exc:
            log.warning("kalshi websocket: could not update subscription (%s); reconnecting", exc)
            await ws.close()

    def _cmd(self) -> int:
        self._next_cmd += 1
        return self._next_cmd

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop = False
            self._thread = threading.Thread(target=self._run, name="kalshi-ws", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop = True
        if self._ws is not None and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        import websockets

        self._loop = asyncio.get_running_loop()
        backoff = 1.0
        while not self._stop:
            try:
                async with websockets.connect(
                    WS_URL, additional_headers=self.client.ws_headers(WS_URL), open_timeout=15, ping_interval=20,
                ) as ws:
                    self._ws = ws
                    self._sids = {}
                    with self._lock:
                        tickers = sorted(self._tickers)
                    self._live = set(tickers)
                    for message in self.subscriptions(tickers):
                        await ws.send(json.dumps(message))
                    self.connected_at, backoff = time.time(), 1.0
                    async for raw in ws:
                        self.handle(raw)
            except Exception as exc:
                log.warning("kalshi websocket: %s; reconnecting in %.0fs", exc, backoff)
            self._ws, self._live, self._sids = None, set(), {}
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def subscriptions(self, tickers: list[str]) -> list[dict[str, Any]]:
        """The subscribe commands sent on every (re)connect."""
        out: list[dict[str, Any]] = []
        if self.index_feed:
            out.append({"id": 1, "cmd": "subscribe", "params": {"channels": ["cfbenchmarks_value"], "index_ids": self.indices}})
            out.append({"id": 5, "cmd": "subscribe", "params": {"channels": ["cfbenchmarks_value_5hz"], "index_ids": self.indices}})
        if self.fills:
            out.append({"id": 3, "cmd": "subscribe", "params": {"channels": ["fill"]}})  # every fill on the account, no ticker list
        if tickers:
            out.extend(self._market_subscriptions(tickers))
        return out

    @staticmethod
    def _market_subscriptions(tickers: list[str]) -> list[dict[str, Any]]:
        """Quotes and public trades are separate subscriptions (each has its own sid, which update_subscription needs); a quote snapshot on subscribe spares waiting for a quiet market's next change."""
        return [
            {"id": 2, "cmd": "subscribe", "params": {"channels": ["ticker"], "market_tickers": tickers, "send_initial_snapshot": True}},
            {"id": 4, "cmd": "subscribe", "params": {"channels": ["trade"], "market_tickers": tickers}},
        ]

    def handle(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if msg.get("type") == "error":
            log.warning("kalshi websocket error: %s", str(msg.get("msg"))[:160])
            if (msg.get("id") or 0) > 100 and self._ws is not None and self._loop is not None:  # a refused update: start over from a clean subscribe
                asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
            return
        if msg.get("type") == "subscribed":
            channel, sid = (msg.get("msg") or {}).get("channel"), (msg.get("msg") or {}).get("sid")
            if channel in {"ticker", "trade"} and isinstance(sid, int):
                self._sids[channel] = sid
                if self._loop is not None and "ticker" in self._sids and "trade" in self._sids:
                    asyncio.run_coroutine_threadsafe(self._sync(), self._loop)  # the wanted set may have moved since the subscribe went out
            return
        event = parse_message(msg)
        if event is not None:
            self.last_event_at = event["ts"]
            self.on_event(event)
