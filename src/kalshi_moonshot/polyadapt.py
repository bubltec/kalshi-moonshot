"""Turn Polymarket US payloads into the dicts the rest of the bot already reads from Kalshi.

A market comes back with a Kalshi ticker and series (`KXGOLD15M-26OCT270845`), dollar bid and ask
strings, and a close time. A book comes back as YES and NO bids. An order comes back with
`order_id`, `fill_count` and `average_fill_price` in the side's own price. Nothing here talks to
the network.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ET = ZoneInfo("America/New_York")

# Symbol on Polymarket's asset id -> the Kalshi prefix the lanes already filter on.
_PREFIX = {
    "btc": "KXBTC", "eth": "KXETH", "sol": "KXSOL", "xrp": "KXXRP", "doge": "KXDOGE",
    "gold": "KXGOLD", "silver": "KXSILVER", "copper": "KXCOPPER", "wti": "KXWTI",
    "brent": "KXBRENT", "natgas": "KXNATGAS", "platinum": "KXPLATINUM", "palladium": "KXPALLADIUM",
}
# Hourly crypto on Kalshi has an empty suffix (`KXBTC`). Hourly metals use `H`. Daily WTI uses
# the empty suffix; every other daily uses `D`.
_CRYPTO = frozenset({"btc", "eth", "sol", "xrp", "doge"})

_UPDOWN = frozenset({"updown", "ASSET_PRICE_MARKET_TYPE_UP_DOWN"})
_STRIKE = frozenset({"strike", "ASSET_PRICE_MARKET_TYPE_ABOVE_BELOW"})
_RANGE = frozenset({"range", "ASSET_PRICE_MARKET_TYPE_RANGE"})


def signing_key(secret: str) -> Ed25519PrivateKey:
    """The file holds the base64 secret from the developer portal. The first 32 bytes sign requests."""
    raw = base64.b64decode(secret.strip())
    if len(raw) < 32:
        raise ValueError("Polymarket secret is shorter than an Ed25519 key")
    return Ed25519PrivateKey.from_private_bytes(raw[:32])


_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def ticker_close(ticker: str) -> datetime | None:
    """The close encoded in a Kalshi-shaped ticker (`KXGOLD15M-26OCT270845` is 08:45 ET that day)."""
    parts = ticker.split("-")
    if len(parts) < 2 or len(parts[1]) < 11:
        return None
    stamp = parts[1]
    month = _MONTHS.get(stamp[2:5])
    if month is None:
        return None
    try:
        return datetime(2000 + int(stamp[0:2]), month, int(stamp[5:7]), int(stamp[7:9]), int(stamp[9:11]), tzinfo=ET)
    except ValueError:
        return None


def series_for(symbol: str, horizon: str) -> str | None:
    """The Kalshi series name for this asset and window, or None when this bot has no such series."""
    prefix = _PREFIX.get(symbol.lower())
    if not prefix:
        return None
    h = horizon.lower()
    if h == "15m":
        return prefix + "15M"
    if h in {"1h", "hourly"}:
        return prefix if symbol.lower() in _CRYPTO else prefix + "H"
    if h in {"1d", "daily"}:
        return prefix if symbol.lower() == "wti" else prefix + "D"
    if h in {"1w", "weekly"}:
        return prefix + "W"
    return None


def _amount(node: Any) -> float | None:
    if node is None or node == "":
        return None
    if isinstance(node, dict):
        node = node.get("value")
    try:
        return float(node)
    except (TypeError, ValueError):
        return None


def _when(node: Any) -> datetime | None:
    """An ISO timestamp or a unix-seconds string, as UTC."""
    if node is None or node == "":
        return None
    if isinstance(node, (int, float)):
        return datetime.fromtimestamp(float(node), UTC)
    text = str(node).strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text), UTC)
    try:
        t = datetime.fromisoformat(text)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _stamp(t: datetime) -> str:
    """Kalshi's close stamp: `26OCT270845` is 08:45 ET on 27 Oct 2026."""
    local = t.astimezone(ET)
    return f"{local:%y}{local:%b}{local:%d}{local:%H}{local:%M}".upper()


def _family(raw: dict[str, Any]) -> dict[str, Any]:
    """Symbol, horizon, family and prices from the typed terms, or from the metadata the docs show."""
    terms = raw.get("assetPriceTerms") or {}
    meta = raw.get("metadata") or {}
    asset = terms.get("asset") or {}
    symbol = (asset.get("symbol") if isinstance(asset, dict) else None) or meta.get("crypto_asset_slug") or meta.get("metals_asset_slug") or ""
    horizon = terms.get("horizon") or meta.get("crypto_horizon") or meta.get("metals_horizon") or ""
    kind = terms.get("marketType") or meta.get("crypto_market_type") or meta.get("metals_market_type") or ""
    return {
        "symbol": str(symbol).lower(),
        "horizon": str(horizon).lower(),
        "kind": str(kind),
        "open": _when(terms.get("windowStart") or meta.get("interval_start") or raw.get("startDate")),
        "close": _when(terms.get("windowEnd") or meta.get("interval_end") or raw.get("endDate")),
        "beat": _amount(terms.get("priceToBeat")) or _amount(meta.get("price_to_beat")),
        "strike": _amount(terms.get("strike")) or _amount(meta.get("outcome_strike")),
        "lower": _amount(terms.get("rangeLower")) or _amount(meta.get("crypto_range_lower")),
        "upper": _amount(terms.get("rangeUpper")) or _amount(meta.get("crypto_range_upper")),
        "settlement": _amount(terms.get("settlementPrice")) or _amount(meta.get("crypto_settlement_price")) or _amount(meta.get("metals_settlement_price")),
    }


def market_to_kalshi(raw: dict[str, Any]) -> dict[str, Any] | None:
    """One Polymarket market as a Kalshi market row, or None when it is not a series this bot trades.

    The slug is kept on `polymarket_slug`. Orders are sent with that slug; the ticker is only for
    the lanes, which key off the series prefix and the close time.
    """
    info = _family(raw)
    series = series_for(info["symbol"], info["horizon"])
    close = info["close"]
    slug = raw.get("slug") or raw.get("symbol") or ""
    if not series or close is None or not slug:
        return None
    kind = info["kind"]
    updown, strike, ranged = kind in _UPDOWN, kind in _STRIKE, kind in _RANGE
    if not (updown or strike or ranged):
        return None
    ticker = f"{series}-{_stamp(close)}"
    floor = cap = None
    strike_type = ""
    if updown and info["beat"] is not None and info["beat"] > 0:
        floor, strike_type = info["beat"], "greater_or_equal"
    elif strike and info["strike"] is not None and info["strike"] > 0:
        floor, strike_type = info["strike"], "greater_or_equal"
        ticker = f"{ticker}-{info['strike']}"
    elif ranged:
        floor, cap, strike_type = info["lower"], info["upper"], "between"
        if floor is not None:
            ticker = f"{ticker}-{floor}"
    bid, ask = _amount(raw.get("bestBidQuote")), _amount(raw.get("bestAskQuote"))
    status = raw.get("status") or ""
    open_ = status == "MARKET_STATUS_OPEN" or (raw.get("active") and not raw.get("closed") and status != "MARKET_STATUS_RESOLVED")
    row: dict[str, Any] = {
        "ticker": ticker,
        "series_ticker": series,
        "event_ticker": f"{series}-{_stamp(close)}",
        "title": raw.get("question") or raw.get("title") or "",
        "yes_sub_title": raw.get("subtitle") or "",
        "rules_primary": raw.get("description") or "",
        "yes_bid_dollars": f"{bid:.4f}" if bid and bid > 0 else "0",
        "yes_ask_dollars": f"{ask:.4f}" if ask and ask > 0 else "0",
        "volume_fp": raw.get("volume") or 0,
        "close_time": close.isoformat(),
        "open_time": info["open"].isoformat() if info["open"] else "",
        "status": "open" if open_ else "settled" if status == "MARKET_STATUS_RESOLVED" else "closed",
        "market_type": "binary",
        "category": "Crypto" if info["symbol"] in _CRYPTO else "Commodities",
        "mutually_exclusive": ranged,
        "polymarket_slug": slug,
        "exchange_index": 0,
    }
    if strike_type:
        row["strike_type"] = strike_type
    if floor is not None:
        row["floor_strike"] = floor
    if cap is not None:
        row["cap_strike"] = cap
    if updown and info["beat"] is not None and info["settlement"] is not None:
        row["result"] = "yes" if info["settlement"] >= info["beat"] else "no"
    return row


def book_to_kalshi(raw: dict[str, Any]) -> dict[str, Any]:
    """YES bids stay YES bids. A YES offer at `p` is a NO bid at `1 - p`, which is how Kalshi lists a book."""
    data = raw.get("marketData") or raw
    yes_rows: list[list[str]] = []
    no_rows: list[list[str]] = []
    for entry in data.get("bids") or []:
        px, qty = _amount(entry.get("px")), _amount(entry.get("qty"))
        if px and qty and px > 0 and qty > 0:
            yes_rows.append([f"{px:.4f}", f"{qty:.2f}"])
    for entry in data.get("offers") or []:
        px, qty = _amount(entry.get("px")), _amount(entry.get("qty"))
        if px and qty and 0 < px < 1 and qty > 0:
            no_rows.append([f"{1.0 - px:.4f}", f"{qty:.2f}"])
    return {"orderbook_fp": {"yes_dollars": yes_rows, "no_dollars": no_rows}}


def order_to_kalshi(raw: dict[str, Any], side: str) -> dict[str, Any]:
    """Fills from a synchronous order, priced in the side that was ordered.

    The request asks for that side by intent (`BUY_SHORT` is buy NO) and sends that side's own
    price, so the fill price is already the side price. A resting order that has not traded yet
    reports a zero fill.
    """
    shares = 0.0
    notional = 0.0
    for exe in raw.get("executions") or []:
        kind = str(exe.get("type") or "")
        if "FILL" not in kind:
            continue
        qty, px = _amount(exe.get("lastShares")), _amount(exe.get("lastPx"))
        if qty and px is not None and qty > 0:
            shares += qty
            notional += qty * px
    order = raw.get("order") or {}
    if shares <= 0:
        shares = _amount(order.get("cumQuantity")) or 0.0
        avg = _amount(order.get("avgPx")) or 0.0
    else:
        avg = notional / shares
    return {
        "order_id": raw.get("id") or order.get("id") or "",
        "fill_count": shares,
        "average_fill_price": avg,
        "side": side,
    }


def positions_to_kalshi(raw: dict[str, Any], ticker_of: dict[str, str]) -> dict[str, Any]:
    """Positive size is YES, negative size is NO, matching `/portfolio/positions`.

    `ticker_of` maps a slug to the Kalshi-shaped ticker. A slug we have not listed yet is kept
    as itself so the position is still visible.
    """
    rows = []
    positions = raw.get("positions") or {}
    for slug, pos in positions.items():
        if not isinstance(pos, dict) or pos.get("expired"):
            continue
        qty = _amount(pos.get("netPositionDecimal"))
        if qty is None or abs(qty) < 0.01:
            continue
        cost = abs(_amount(pos.get("cost")) or 0.0)
        meta = pos.get("marketMetadata") or {}
        key = meta.get("slug") or slug
        rows.append({
            "ticker": ticker_of.get(key, key),
            "position_fp": f"{qty:.2f}",
            "market_exposure_dollars": f"{cost:.4f}",
        })
    return {"market_positions": rows}


def balance_to_kalshi(raw: dict[str, Any]) -> dict[str, Any]:
    """Buying power as one shard. Polymarket does not split cash across exchange indexes."""
    rows = raw.get("balances") or []
    usd = next((r for r in rows if str(r.get("currency", "USD")).upper() == "USD"), rows[0] if rows else {})
    power = usd.get("buyingPower")
    if power is None:
        power = usd.get("currentBalance") or 0
    return {"balance_breakdown": [{"balance": f"{float(power):.4f}", "exchange_index": 0}]}
