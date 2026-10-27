"""Polymarket US, speaking the same shapes Kalshi does.

Public market data comes from the gateway with no key. Orders, positions and balances are signed
with the secret in `POLYMARKET_PRIVATE_KEY_PATH` (the portal shows it once; the file holds that
line). The key id is `POLYMARKET_API_KEY_ID`. Every method the scan and the watcher call returns
a Kalshi-shaped dict; see polyadapt.py.
"""

from __future__ import annotations

import base64
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .kalshi import KalshiError
from .marketdata import MarketCache
from .polyadapt import (
    balance_to_kalshi,
    book_to_kalshi,
    market_to_kalshi,
    order_to_kalshi,
    positions_to_kalshi,
    signing_key,
    ticker_close,
)

GATEWAY = "https://gateway.polymarket.us"
API = "https://api.polymarket.us"
RETRIES = 5


class PolymarketError(KalshiError):
    """A Polymarket call failed. It is a KalshiError so the scan's existing handlers catch it."""


def load_secret(path: str) -> str:
    """The one line in the key file, with surrounding whitespace removed."""
    return Path(path).read_text().strip()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class PolymarketClient:
    def __init__(
        self, key_id: str = "", private_key_path: str = "", max_rps: float = 10.0,
        gateway: str = GATEWAY, api: str = API,
    ):
        self.key_id = key_id.strip()
        self._key = signing_key(load_secret(private_key_path)) if private_key_path and key_id else None
        self.gateway = gateway.rstrip("/")
        self.api = api.rstrip("/")
        self.page_size = 200
        self._http = httpx.Client(timeout=20.0)
        self._gap = 1.0 / max(0.5, max_rps)
        self._next_slot = 0.0
        self._lock = threading.Lock()
        self.cache = MarketCache()
        self._tl = threading.local()
        self._slug_of: dict[str, str] = {}  # Kalshi-shaped ticker -> Polymarket slug
        self._ticker_of: dict[str, str] = {}
        self._events: dict[str, tuple[str, str, bool]] = {}
        self._order_slug: dict[str, str] = {}
        self._listed: tuple[tuple[int, int], float, list[dict[str, Any]]] | None = None

    @property
    def authed(self) -> bool:
        return self._key is not None and bool(self.key_id)

    @contextmanager
    def shared(self, max_age: float) -> Iterator[None]:
        prev = getattr(self._tl, "max_age", 0.0)
        self._tl.max_age = max_age
        try:
            yield
        finally:
            self._tl.max_age = prev

    def _headers(self, method: str, path: str) -> dict[str, str]:
        key = self._key
        if key is None or not self.key_id:
            raise PolymarketError("this call needs POLYMARKET_API_KEY_ID and POLYMARKET_PRIVATE_KEY_PATH")
        ts = str(int(time.time() * 1000))
        sig = base64.b64encode(key.sign(f"{ts}{method.upper()}{path}".encode())).decode()
        return {
            "X-PM-Access-Key": self.key_id,
            "X-PM-Timestamp": ts,
            "X-PM-Signature": sig,
            "Content-Type": "application/json",
        }

    def _pace(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._gap
        delay = slot - now
        if delay > 0:
            time.sleep(delay)

    def _request(self, method: str, path: str, *, base: str, params=None, json=None, auth: bool = False) -> Any:
        last = ""
        for attempt in range(RETRIES):
            self._pace()
            headers = self._headers(method, path) if auth else {}
            try:
                resp = self._http.request(method, base + path, params=params, json=json, headers=headers)
            except httpx.TransportError as exc:
                last = f"{type(exc).__name__}: {exc}"
                if method.upper() != "GET" or attempt == RETRIES - 1:
                    raise PolymarketError(f"{method} {path}: {last}") from exc
                time.sleep(0.5 * (2**attempt))
                continue
            if resp.status_code == 429 and attempt < RETRIES - 1:
                time.sleep(0.5 * (2**attempt))
                continue
            if resp.status_code >= 400:
                raise PolymarketError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
            return resp.json() if resp.content else {}
        raise PolymarketError(f"{method} {path}: {last}")

    def _remember(self, row: dict[str, Any]) -> None:
        slug = row.get("polymarket_slug") or ""
        ticker = row.get("ticker") or ""
        if slug and ticker:
            self._slug_of[ticker] = slug
            self._ticker_of[slug] = ticker
        event = row.get("event_ticker") or ""
        if event:
            self._events[event] = (row.get("category") or "", row.get("series_ticker") or "", bool(row.get("mutually_exclusive")))

    def _slug(self, ticker: str) -> str:
        slug = self._slug_of.get(ticker)
        if slug:
            return slug
        raise PolymarketError(f"no Polymarket slug for {ticker}; list the market again before ordering")

    def event_info(self, ttl: float = 3600.0, pages: int = 60) -> dict[str, tuple[str, str, bool]]:
        """The same dict markets_closing fills, so a scan that reads it while listing sees each event."""
        del ttl, pages
        return self._events

    def fill_event_info(self, tickers: list[str]) -> dict[str, tuple[str, str, bool]]:
        return self.event_info()

    def markets_closing(self, min_ts: float, max_ts: float, pages: int = 20, page_size: int | None = None) -> Iterator[dict[str, Any]]:
        """Open markets whose end falls in the window, already in Kalshi's shape. Others are skipped."""
        size = page_size or self.page_size
        for page in range(pages):
            body = self._request("GET", "/v1/markets", base=self.gateway, params={
                "active": True, "closed": False, "limit": size, "offset": page * size,
                "endDateMin": _iso(min_ts), "endDateMax": _iso(max_ts),
            })
            markets = body.get("markets") or []
            if not markets:
                return
            for raw in markets:
                row = market_to_kalshi(raw)
                if row is None or row["status"] not in {"active", "open"}:
                    continue
                self._remember(row)
                self.cache.put_market(row["ticker"], row)
                yield row
            if len(markets) < size:
                return

    def _listed_rows(self, min_ts: float, max_ts: float) -> list[dict[str, Any]]:
        """One listing of a close-time window, reused for a few seconds so a scope of eight series is one fetch."""
        key = (int(min_ts) // 60, int(max_ts) // 60)
        hit = self._listed
        if hit is not None and hit[0] == key and time.time() - hit[1] < 20:
            return hit[2]
        rows = list(self.markets_closing(min_ts, max_ts))
        self._listed = (key, time.time(), rows)
        return rows

    def series_markets(self, series: str) -> list[dict[str, Any]]:
        """Open markets of one series. Listed windows are only a day or so ahead, so the window is short."""
        age = float(getattr(self._tl, "max_age", 0.0))
        if age > 0 and (hit := self.cache.get_series(series, age)) is not None:
            return hit
        now = time.time()
        rows = [m for m in self._listed_rows(now - 3600, now + 36 * 3600) if m.get("series_ticker") == series]
        self.cache.put_series(series, rows)
        return rows

    def event_markets(self, event_ticker: str) -> list[dict[str, Any]]:
        """The markets of one window. An up/down window is a single contract."""
        age = float(getattr(self._tl, "max_age", 0.0))
        if age > 0 and (hit := self.cache.get_event(event_ticker, age)) is not None:
            return hit
        series = event_ticker.split("-")[0]
        rows = [m for m in self.series_markets(series) if m.get("event_ticker") == event_ticker]
        self.cache.put_event(event_ticker, rows)
        return rows

    def market(self, ticker: str) -> dict[str, Any]:
        age = float(getattr(self._tl, "max_age", 0.0))
        if age > 0 and (hit := self.cache.get_market(ticker, age)) is not None:
            return hit
        slug = self._slug_of.get(ticker, "")
        if not slug:
            close = ticker_close(ticker)
            if close is None:
                raise PolymarketError(f"no Polymarket slug for {ticker}")
            ts = close.timestamp()
            for row in self.markets_closing(ts - 90, ts + 90):
                if row["ticker"] == ticker:
                    return row
            raise PolymarketError(f"no Polymarket market for {ticker}")
        body = self._request("GET", f"/v1/markets/{slug}", base=self.gateway)
        raw = body.get("market") or body
        row = market_to_kalshi(raw)
        if row is None:
            raise PolymarketError(f"{ticker} is not a series this bot trades")
        self._remember(row)
        self.cache.put_market(row["ticker"], row)
        return row

    def orderbook(self, ticker: str) -> dict[str, Any]:
        body = self._request("GET", f"/v1/markets/{self._slug(ticker)}/book", base=self.gateway)
        return book_to_kalshi(body)

    def balance(self) -> dict[str, Any]:
        return balance_to_kalshi(self._request("GET", "/v1/account/balances", base=self.api, auth=True))

    def positions(self) -> dict[str, Any]:
        body = self._request("GET", "/v1/portfolio/positions", base=self.api, auth=True)
        return positions_to_kalshi(body, self._ticker_of)

    def _order(self, ticker: str, side: str, action: str, count: float, price: float, *, tif: str, seconds: float | None, post_only: bool) -> dict[str, Any]:
        """`price` is the side's own price. BUY_SHORT / SELL_SHORT are the NO intents, so it is not inverted."""
        intent = {
            ("yes", "buy"): "ORDER_INTENT_BUY_LONG",
            ("yes", "sell"): "ORDER_INTENT_SELL_LONG",
            ("no", "buy"): "ORDER_INTENT_BUY_SHORT",
            ("no", "sell"): "ORDER_INTENT_SELL_SHORT",
        }[(side, action)]
        body: dict[str, Any] = {
            "marketSlug": self._slug(ticker),
            "intent": intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{price:.4f}", "currency": "USD"},
            "quantity": count,
            "tif": tif,
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "synchronousExecution": True,
            "maxBlockTime": "2",
        }
        if post_only:
            body["participateDontInitiate"] = True
        if seconds is not None:
            body["goodTillTime"] = _iso(time.time() + seconds)
        resp = self._request("POST", "/v1/orders", base=self.api, json=body, auth=True)
        out = order_to_kalshi(resp, side)
        if out["order_id"]:
            self._order_slug[out["order_id"]] = body["marketSlug"]
        return out

    def buy_yes_ioc(self, ticker: str, count: float, price_dollars: float, client_order_id: str | None = None) -> dict[str, Any]:
        del client_order_id
        return self._order(ticker, "yes", "buy", count, price_dollars, tif="TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", seconds=None, post_only=False)

    def buy_no_ioc(self, ticker: str, count: float, no_price: float, client_order_id: str | None = None) -> dict[str, Any]:
        del client_order_id
        return self._order(ticker, "no", "buy", count, no_price, tif="TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", seconds=None, post_only=False)

    def sell_yes_ioc(self, ticker: str, count: float, price_dollars: float, client_order_id: str | None = None) -> dict[str, Any]:
        del client_order_id
        return self._order(ticker, "yes", "sell", count, price_dollars, tif="TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", seconds=None, post_only=False)

    def sell_no_ioc(self, ticker: str, count: float, no_price: float, client_order_id: str | None = None) -> dict[str, Any]:
        del client_order_id
        return self._order(ticker, "no", "sell", count, no_price, tif="TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", seconds=None, post_only=False)

    def buy_resting(self, ticker: str, side: str, count: float, price: float, seconds: float, client_order_id: str | None = None, post_only: bool = True) -> dict[str, Any]:
        del client_order_id
        return self._order(ticker, side, "buy", count, price, tif="TIME_IN_FORCE_GOOD_TILL_DATE", seconds=seconds, post_only=post_only)

    def sell_resting(self, ticker: str, side: str, count: float, price: float, seconds: float, client_order_id: str | None = None, post_only: bool = True) -> dict[str, Any]:
        del client_order_id
        return self._order(ticker, side, "sell", count, price, tif="TIME_IN_FORCE_GOOD_TILL_DATE", seconds=seconds, post_only=post_only)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        slug = self._order_slug.get(order_id, "")
        path = f"/v1/order/{order_id}/cancel"
        self._request("POST", path, base=self.api, json={"marketSlug": slug} if slug else {}, auth=True)
        return {"order_id": order_id}


def client_from_env() -> PolymarketClient:
    """A client from POLYMARKET_API_KEY_ID and POLYMARKET_PRIVATE_KEY_PATH. Public reads work with both empty."""
    import os
    return PolymarketClient(
        key_id=os.environ.get("POLYMARKET_API_KEY_ID", ""),
        private_key_path=os.environ.get("POLYMARKET_PRIVATE_KEY_PATH", ""),
    )
