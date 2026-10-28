"""The only module that talks to Kalshi. Public market data needs no key; the rest is signed."""

from __future__ import annotations

import base64
import logging
import random
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import reqstats, steptime
from .marketdata import MarketCache

log = logging.getLogger("moonshot")
_EVENT_CACHE: dict[str, tuple[float, dict[str, tuple[str, str, bool]]]] = {}  # base url -> (when listed, event_ticker -> info), shared by every client and thread
_EVENT_LOCK = threading.Lock()
SLOW_PACE_SECONDS = 3.0  # a request that waits this long for its turn is logged, so a queue behind the shared request budget shows in the logs


class KalshiError(RuntimeError):
    pass


class KalshiRateLimited(KalshiError):
    """Still 429 after every retry. retry_after is how long to wait before trying again."""

    def __init__(self, message: str, retry_after: float = 30.0):
        super().__init__(message)
        self.retry_after = retry_after


RETRIES = 5  # tries per request
MAX_BACKOFF = 30.0  # seconds


def _event_tuple(e: dict[str, Any]) -> tuple[str, str, bool]:
    return (
        (e.get("category") or "").strip(),
        e.get("series_ticker", "") or "",
        bool(e.get("mutually_exclusive")),
    )


# Page cap, not page size. Size comes from KALSHI_PAGE_SIZE (default 1000).
# A 1-72h window held 28,186 markets on 2026-10-18.
MARKET_PAGES = 200


def _as_no(resp: dict[str, Any]) -> dict[str, Any]:
    """Express a YES-book fill price as the NO price (1 - price). Other fields are unchanged."""
    out = dict(resp)
    if out.get("average_fill_price") not in (None, "", 0, "0"):
        out["average_fill_price"] = 1.0 - float(out["average_fill_price"])
    return out


def load_private_key(path: str):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign(private_key, text: str) -> str:
    """Ed25519 signs the text directly; RSA uses RSA-PSS with SHA-256."""
    msg = text.encode()
    if isinstance(private_key, Ed25519PrivateKey):
        sig = private_key.sign(msg)
    else:
        sig = private_key.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    return base64.b64encode(sig).decode()


def signed_headers(private_key, key_id: str, method: str, url: str, ts_ms: int | None = None):
    """The signed text is timestamp + METHOD + path, with the query string removed."""
    path = urlparse(url).path
    ts = str(ts_ms if ts_ms is not None else int(time.time() * 1000))
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sign(private_key, ts + method.upper() + path),
    }


class KalshiClient:
    def __init__(
        self, base_url: str, key_id: str = "", private_key_path: str = "",
        max_rps: float = 8.0, page_size: int = 1000,
    ):
        self.base_url = base_url.rstrip("/")
        self.key_id = key_id
        self.page_size = min(1000, max(1, page_size))
        self._key = load_private_key(private_key_path) if private_key_path else None
        self._http = httpx.Client(timeout=20.0)
        self._gap = 1.0 / max(0.5, max_rps)  # minimum seconds between requests
        self._next_slot = 0.0
        self._static: dict[str, tuple[float, dict[str, Any]]] = {}
        self._next_write_slot = 0.0  # orders and cancels queue apart from reads: Kalshi limits writes separately, and a sale must not wait behind a page of market reads
        self._lock = threading.Lock()
        self.cache = MarketCache()  # every listing fetched is kept here for the lanes that read it (marketdata.py)
        self._tl = threading.local()

    def set_rate(self, max_rps: float, page_size: int) -> None:
        """Change the request rate and page size of this client in place. Every thread holds this same client, so a new client with the old one closed would leave them all failing with 'client has been closed'."""
        self._gap = 1.0 / max(0.5, max_rps)
        self.page_size = min(1000, max(1, page_size))

    @contextmanager
    def shared(self, max_age: float) -> Iterator[None]:
        """Inside this block, market listings and single markets may come from the shared cache when no older than max_age seconds (0 or less: always ask Kalshi)."""
        prev = getattr(self._tl, "max_age", 0.0)
        self._tl.max_age = max_age
        try:
            yield
        finally:
            self._tl.max_age = prev

    def _age(self) -> float:
        return float(getattr(self._tl, "max_age", 0.0))

    @contextmanager
    def background(self) -> Iterator[None]:
        """Inside this block this thread's requests are throttled to a third of the budget, so a long maintenance job (history, resolving) cannot crowd out a live order."""
        prev = getattr(self._tl, "bg", False)
        self._tl.bg = True
        try:
            yield
        finally:
            self._tl.bg = prev

    def ws_headers(self, url: str) -> dict[str, str]:
        """Signed headers for opening an authenticated WebSocket (a read-only data stream)."""
        if not self.authed:
            raise KalshiError("the WebSocket needs KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
        return signed_headers(self._key, self.key_id, "GET", url)

    @property
    def authed(self) -> bool:
        return bool(self._key and self.key_id)

    def _pace(self, extra: float = 0.0, write: bool = False) -> None:
        """Space requests out so a long paging run does not hit the rate limit at all.

        extra pushes every later request back too, which is how a 429 slows the whole client
        down instead of just the one call that got it.
        """
        with self._lock:
            now = time.monotonic()
            if write:
                slot = max(now, self._next_write_slot) + extra
                self._next_write_slot = slot + self._gap
            else:
                slot = max(now, self._next_slot) + extra
                self._next_slot = slot + self._gap
        delay = slot - now
        if delay > 0:
            steptime.add("request wait", delay)
        if delay > SLOW_PACE_SECONDS:
            log.info("request waited %.1fs for its turn under the shared request budget", delay)
        if delay > 0:
            time.sleep(delay)

    @staticmethod
    def _backoff(attempt: int, retry_after: float | None) -> float:
        base = retry_after if retry_after is not None else min(MAX_BACKOFF, 0.5 * (2**attempt))
        return min(MAX_BACKOFF, base) * (1 + random.random() * 0.25)  # jitter

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        try:
            return max(0.0, float(resp.headers.get("Retry-After", "")))
        except ValueError:
            return None

    def _request(self, method: str, path: str, *, params=None, json=None, auth=False) -> Any:
        """One call, paced. Retries 429 always (the request was refused, so it is safe).

        Server errors and timeouts are retried for reads only. A timed-out order may have been
        accepted, so POST is never re-sent after one; the caller decides what to do.
        """
        url = self.base_url + path
        safe = method.upper() == "GET"
        if auth and not self.authed:
            raise KalshiError("this call needs KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
        last_wait = 0.0
        for attempt in range(RETRIES):
            headers = signed_headers(self._key, self.key_id, method, url) if auth else {}
            if getattr(self._tl, "bg", False):
                time.sleep(2 * self._gap)  # outside the pacing lock: the slots it leaves free go to the threads that are not in the background
            self._pace(write=not safe)
            asked = time.perf_counter()
            reqstats.count(method, path)
            try:
                resp = self._http.request(method, url, params=params, json=json, headers=headers)
                steptime.add("kalshi requests", time.perf_counter() - asked)
            except httpx.TransportError as exc:  # timeout, connection reset, DNS
                steptime.add("kalshi requests", time.perf_counter() - asked)
                if not safe or attempt == RETRIES - 1:
                    raise KalshiError(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
                time.sleep(self._backoff(attempt, None))
                continue
            if resp.status_code == 429:
                last_wait = self._backoff(attempt, self._retry_after(resp))
                log.warning("%s %s was refused with 429 (try %d/%d); this call retries in %.1fs", method, path, attempt + 1, RETRIES, last_wait)
                time.sleep(last_wait)  # only this caller waits out the 429, then takes its normal turn again: it must not hold every other thread's requests back (a sale would wait behind a read's backoff)
                continue
            if resp.status_code >= 500 and safe and attempt < RETRIES - 1:
                time.sleep(self._backoff(attempt, None))
                continue
            if resp.status_code >= 400:
                raise KalshiError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise KalshiRateLimited(f"{method} {path}: rate limited", retry_after=max(30.0, last_wait))

    # ---- public market data ----
    def open_markets(self, pages: int = 5, page_size: int | None = None) -> Iterator[dict[str, Any]]:
        """Open single-market (non-combo) markets, newest cursor pages first."""
        page_size = page_size or self.page_size
        cursor = ""
        for _ in range(pages):
            params: dict[str, Any] = {
                "status": "open",
                "mve_filter": "exclude",
                "limit": page_size,
            }
            if cursor:
                params["cursor"] = cursor
            body = self._request("GET", "/markets", params=params)
            yield from body.get("markets", [])
            cursor = body.get("cursor") or ""
            if not cursor:
                return

    def markets_closing(
        self, min_ts: float, max_ts: float, pages: int = MARKET_PAGES, page_size: int | None = None
    ) -> Iterator[dict[str, Any]]:
        """Open markets whose close time falls in [min_ts, max_ts] (unix seconds).

        Neither the plain market list nor the event list is ordered by close time, so a
        short scan of either can miss every market that closes inside a short window.
        Asking Kalshi for the window directly does not.
        """
        page_size = page_size or self.page_size
        cursor = ""
        for _ in range(pages):
            params: dict[str, Any] = {
                "status": "open",
                "mve_filter": "exclude",
                "limit": page_size,
                "min_close_ts": int(min_ts),
                "max_close_ts": int(max_ts),
            }
            if cursor:
                params["cursor"] = cursor
            body = self._request("GET", "/markets", params=params)
            yield from body.get("markets", [])
            cursor = body.get("cursor") or ""
            if not cursor:
                return

    def event_markets(self, event_ticker: str) -> list[dict[str, Any]]:
        """Every open market in one event (e.g. a whole strike ladder)."""
        if self._age() > 0 and (hit := self.cache.get_event(event_ticker, self._age())) is not None:
            return hit
        out: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(5):
            params: dict[str, Any] = {"event_ticker": event_ticker, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            body = self._request("GET", "/markets", params=params)
            out.extend(m for m in body.get("markets", []) if m.get("status") in {"active", "open"})
            cursor = body.get("cursor") or ""
            if not cursor:
                break
        self.cache.put_event(event_ticker, out)
        return out

    def series_markets(self, series: str) -> list[dict[str, Any]]:
        """Every open market in one series (a handful of requests, not a full-exchange scan)."""
        if self._age() > 0 and (hit := self.cache.get_series(series, self._age())) is not None:
            return hit
        out: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(5):
            params: dict[str, Any] = {"series_ticker": series, "status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            body = self._request("GET", "/markets", params=params)
            out.extend(body.get("markets", []))
            cursor = body.get("cursor") or ""
            if not cursor:
                break
        if not out:
            # The server-side status filter returned nothing while the unfiltered list showed an active market (2026-10-24, 15-minute series).
            # An empty answer is also the normal one for a closed series, so ask once more without the filter and keep the open markets ourselves.
            body = self._request("GET", "/markets", params={"series_ticker": series, "limit": 1000})
            out = [m for m in body.get("markets", []) if m.get("status") in {"active", "open"}]
        self.cache.put_series(series, out)
        return out

    def settled_markets(self, series: str, limit: int = 40) -> list[dict[str, Any]]:
        """The most recent settled markets of a series (each carries its settlement value)."""
        params = {"series_ticker": series, "status": "settled", "limit": limit}
        return self._request("GET", "/markets", params=params).get("markets", [])

    def series(self, ticker: str) -> dict[str, Any]:
        return self._request("GET", f"/series/{ticker}").get("series") or {}

    def event(self, ticker: str) -> dict[str, Any]:
        return self._request("GET", f"/events/{ticker}").get("event") or {}

    def event_info(self, ttl: float = 3600.0, pages: int = 60) -> dict[str, tuple[str, str, bool]]:
        """event_ticker -> (category, series_ticker, mutually_exclusive). One cache for the whole process, filled by one thread at a time: the listing is a few pages, but every thread (and every client) that found
        the cache empty used to page it on its own, so a restart made about 40 full listings in a minute."""
        cached = _EVENT_CACHE.get(self.base_url)
        if cached and time.time() - cached[0] < ttl:
            return cached[1]
        with _EVENT_LOCK:
            cached = _EVENT_CACHE.get(self.base_url)  # another thread may have filled it while this one waited
            if cached and time.time() - cached[0] < ttl:
                return cached[1]
            info: dict[str, tuple[str, str, bool]] = {}
            cursor = ""
            for _ in range(pages):
                params: dict[str, Any] = {"status": "open", "limit": min(self.page_size, 200)}
                if cursor:
                    params["cursor"] = cursor
                body = self._request("GET", "/events", params=params)
                for e in body.get("events", []):
                    info[e["event_ticker"]] = _event_tuple(e)
                cursor = body.get("cursor") or ""
                if not cursor:
                    break
            _EVENT_CACHE[self.base_url] = (time.time(), info)
            return info

    def fill_event_info(self, tickers: list[str]) -> dict[str, tuple[str, str, bool]]:
        """Like event_info, plus a one-off fetch for any ticker the cache missed.

        The events list is not ordered by close time, so the cache can miss a game that is
        about to settle. Looking up just the shortlist is cheap and is how we learn whether
        two markets are mutually exclusive.
        """
        info = dict(self.event_info())
        missing = [t for t in dict.fromkeys(tickers) if t and t not in info]
        for ticker in missing:
            try:
                e = self.event(ticker)
            except KalshiError:
                continue
            if e.get("event_ticker") or ticker:
                info[e.get("event_ticker") or ticker] = _event_tuple(e)
        shared = _EVENT_CACHE.get(self.base_url)
        if shared:
            shared[1].update(info)  # what was looked up one by one is kept for the next caller; the cache keeps its own age
        return info

    def candlesticks(self, series: str, ticker: str, hours: int = 6) -> list[dict[str, Any]]:
        """Hourly ask/bid history. Kalshi's own previous_* price fields are mostly zero."""
        now = int(time.time())
        body = self._request(
            "GET",
            f"/series/{series}/markets/{ticker}/candlesticks",
            params={"start_ts": now - hours * 3600, "end_ts": now, "period_interval": 60},
        )
        return body.get("candlesticks", [])

    def market_static(self, ticker: str, ttl: float = 600.0) -> dict[str, Any]:
        """A market read for the fields that never change (close time, strike, title), kept for `ttl` seconds. The watcher asked for the same market on every 3-second refresh."""
        hit = self._static.get(ticker)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        row = self.market(ticker)
        self._static[ticker] = (time.time(), row)
        if len(self._static) > 500:
            for old in sorted(self._static, key=lambda t: self._static[t][0])[:100]:
                self._static.pop(old, None)
        return row

    def markets_by_tickers(self, tickers: list[str]) -> list[dict[str, Any]]:
        """Up to 100 markets in one request (any status, settled ones included)."""
        if not tickers:
            return []
        body = self._request("GET", "/markets", params={"tickers": ",".join(tickers[:100]), "limit": min(100, len(tickers))})
        return list(body.get("markets", []))

    def market(self, ticker: str) -> dict[str, Any]:
        if self._age() > 0 and (hit := self.cache.get_market(ticker, self._age())) is not None:
            return hit
        row = self._request("GET", f"/markets/{ticker}")["market"]
        self.cache.put_market(ticker, row)
        return row

    def orderbook(self, ticker: str) -> dict[str, Any]:
        return self._request("GET", f"/markets/{ticker}/orderbook")

    # ---- account (signed) ----
    def balance(self) -> dict[str, Any]:
        return self._request("GET", "/portfolio/balance", auth=True)

    def shard_balances(self) -> dict[int, float]:
        """Cash per exchange shard, in dollars."""
        rows = self.balance().get("balance_breakdown") or []
        return {int(r.get("exchange_index", -1)): float(r.get("balance") or 0.0) for r in rows}

    def shard_transfer(
        self, usd: float, source: str = "event_contract", source_shard: int = 2,
        destination: str = "event_contract", destination_shard: int = 0, transfer_id: str | None = None,
    ) -> dict[str, Any]:
        """Move cash within your own account between exchange shards (POST /portfolio/intra_exchange_instance_transfer).

        Kalshi keeps a cash balance per exchange shard, and an API order can only spend the balance on its market's shard.
        Amounts are in centicents (1/10000 of a dollar). The transfer is accepted now and processed asynchronously.
        """
        body = {
            "source": source, "destination": destination, "amount": round(usd * 10_000),
            "source_exchange_shard": source_shard, "destination_exchange_shard": destination_shard,
            "client_transfer_id": transfer_id or str(uuid.uuid4()),
        }
        return self._request("POST", "/portfolio/intra_exchange_instance_transfer", json=body, auth=True)

    def portfolio_rows(self, path: str, key: str, min_ts: int | None = None, pages: int = 60) -> list[dict[str, Any]]:
        """Every row of a paged portfolio list (fills, settlements, orders), newest first."""
        out: list[dict[str, Any]] = []
        cursor = ""
        for _ in range(pages):
            params: dict[str, Any] = {"limit": 200}
            if min_ts is not None:
                params["min_ts"] = int(min_ts)
            if cursor:
                params["cursor"] = cursor
            body = self._request("GET", path, params=params, auth=True)
            out.extend(body.get(key, []))
            cursor = body.get("cursor") or ""
            if not cursor:
                break
        return out

    def positions(self) -> dict[str, Any]:
        return self._request("GET", "/portfolio/positions", auth=True)

    def buy_yes_ioc(
        self, ticker: str, count: float, price_dollars: float, client_order_id: str | None = None
    ) -> dict[str, Any]:
        """Buy YES, immediate-or-cancel: whatever does not fill right now is dropped,
        so no resting order is ever left on the book.

        Pass a stable client_order_id when the caller may retry after a crash, so Kalshi sees
        the same order twice instead of two orders.
        """
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": "bid",
            "count": f"{count:.2f}",
            "price": f"{price_dollars:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
        }
        return self._request("POST", "/portfolio/events/orders", json=body, auth=True)

    def buy_resting(
        self, ticker: str, side: str, count: float, price: float, seconds: float, client_order_id: str | None = None, post_only: bool = True
    ) -> dict[str, Any]:
        """Buy `side` ("yes" or "no") at `price` (that side's own price) as a resting order that cancels itself after `seconds`.

        Kalshi charges its trading fee to an order that matches at once; an order that rests on the book first pays none on a standard series (fee type `quadratic`).
        `post_only` makes Kalshi refuse the order instead of letting it cross the book, where it would be charged as a taker.
        NO is sent the way buy_no_ioc sends it: as an ask on the YES book at 1 - price.
        """
        body: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": "bid" if side == "yes" else "ask",
            "count": f"{count:.2f}",
            "price": f"{price if side == 'yes' else 1.0 - price:.4f}",
            "time_in_force": "good_till_canceled",
            "expiration_time": int(time.time() + seconds),
            "post_only": post_only,
            "self_trade_prevention_type": "taker_at_cross",
        }
        resp = self._request("POST", "/portfolio/events/orders", json=body, auth=True)
        return resp if side == "yes" else _as_no(resp)

    def sell_resting(
        self, ticker: str, side: str, count: float, price: float, seconds: float, client_order_id: str | None = None, post_only: bool = True
    ) -> dict[str, Any]:
        """Sell `side` ("yes" or "no") at `price` (that side's own price) as a resting order that cancels itself after `seconds`.

        The mirror of buy_resting: an order that rests on the book first is charged no trading fee on a standard
        series (fee type `quadratic`); only an order that matches at once pays. `post_only` makes Kalshi refuse the
        order instead of letting it cross the book, where it would be charged as a taker: a target the bid has already
        reached is refused here and left to the watcher's immediate sale. reduce_only so it can only ever close the
        position it was placed against. NO is sent the way sell_no_ioc sends it: as a bid on the YES book at 1 - price.
        """
        body: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": "ask" if side == "yes" else "bid",
            "count": f"{count:.2f}",
            "price": f"{price if side == 'yes' else 1.0 - price:.4f}",
            "time_in_force": "good_till_canceled",
            "expiration_time": int(time.time() + seconds),
            "post_only": post_only,
            "self_trade_prevention_type": "taker_at_cross",
            "reduce_only": True,
        }
        resp = self._request("POST", "/portfolio/events/orders", json=body, auth=True)
        return resp if side == "yes" else _as_no(resp)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel one open order by id. Kalshi answers with the order's last state; a fill that beat the cancel is on it (and in the fills list)."""
        return self._request("DELETE", f"/portfolio/events/orders/{order_id}", auth=True)

    def buy_no_ioc(
        self, ticker: str, count: float, no_price: float, client_order_id: str | None = None
    ) -> dict[str, Any]:
        """Buy NO at no_price, immediate-or-cancel.

        Kalshi's order endpoint quotes the YES book only: `ask` sells YES, which its docs call
        economically the same as buying NO at 1 - price. The response's fill price is a YES
        price, so it is flipped back to the NO price the caller asked in.
        """
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": "ask",
            "count": f"{count:.2f}",
            "price": f"{1.0 - no_price:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
        }
        return _as_no(self._request("POST", "/portfolio/events/orders", json=body, auth=True))

    def sell_no_ioc(
        self, ticker: str, count: float, no_price: float, client_order_id: str | None = None
    ) -> dict[str, Any]:
        """Sell NO we hold: buy YES back at 1 - no_price, reduce_only so it cannot open a YES."""
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": "bid",
            "count": f"{count:.2f}",
            "price": f"{1.0 - no_price:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "reduce_only": True,
        }
        return _as_no(self._request("POST", "/portfolio/events/orders", json=body, auth=True))

    def sell_yes_ioc(
        self, ticker: str, count: float, price_dollars: float, client_order_id: str | None = None
    ) -> dict[str, Any]:
        """Sell YES, immediate-or-cancel. reduce_only so this cannot open a short if we
        already flattened the position. Same book as buy_yes_ioc: ask = sell YES.
        """
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": "ask",
            "count": f"{count:.2f}",
            "price": f"{price_dollars:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "reduce_only": True,
        }
        return self._request("POST", "/portfolio/events/orders", json=body, auth=True)
