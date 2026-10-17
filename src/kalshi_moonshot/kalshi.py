"""The only module that talks to Kalshi. Public market data needs no key; the rest is signed."""

from __future__ import annotations

import base64
import time
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class KalshiError(RuntimeError):
    pass


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
    def __init__(self, base_url: str, key_id: str = "", private_key_path: str = ""):
        self.base_url = base_url.rstrip("/")
        self.key_id = key_id
        self._key = load_private_key(private_key_path) if private_key_path else None
        self._http = httpx.Client(timeout=20.0)

    @property
    def authed(self) -> bool:
        return bool(self._key and self.key_id)

    def _request(self, method: str, path: str, *, params=None, json=None, auth=False) -> Any:
        url = self.base_url + path
        headers: dict[str, str] = {}
        if auth:
            if not self.authed:
                raise KalshiError("this call needs KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
            headers = signed_headers(self._key, self.key_id, method, url)
        for attempt in range(4):
            resp = self._http.request(method, url, params=params, json=json, headers=headers)
            if resp.status_code == 429:  # token bucket empty; back off and retry
                time.sleep(0.5 * (2**attempt))
                if auth:
                    headers = signed_headers(self._key, self.key_id, method, url)
                continue
            if resp.status_code >= 400:
                raise KalshiError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise KalshiError(f"{method} {path}: rate limited")

    # ---- public market data ----
    def open_markets(self, pages: int = 5, page_size: int = 200) -> Iterator[dict[str, Any]]:
        """Open single-market (non-combo) markets, newest cursor pages first."""
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

    def market(self, ticker: str) -> dict[str, Any]:
        return self._request("GET", f"/markets/{ticker}")["market"]

    def orderbook(self, ticker: str) -> dict[str, Any]:
        return self._request("GET", f"/markets/{ticker}/orderbook")

    # ---- account (signed) ----
    def balance(self) -> dict[str, Any]:
        return self._request("GET", "/portfolio/balance", auth=True)

    def positions(self) -> dict[str, Any]:
        return self._request("GET", "/portfolio/positions", auth=True)

    def buy_yes_ioc(self, ticker: str, count: int, price_dollars: float) -> dict[str, Any]:
        """Buy YES, immediate-or-cancel: whatever does not fill right now is dropped,
        so no resting order is ever left on the book."""
        body = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": "bid",
            "count": f"{count:.2f}",
            "price": f"{price_dollars:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
        }
        return self._request("POST", "/portfolio/events/orders", json=body, auth=True)
