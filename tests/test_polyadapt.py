import base64
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kalshi_moonshot.manual import parse_positions
from kalshi_moonshot.polyadapt import (
    balance_to_kalshi,
    book_to_kalshi,
    market_to_kalshi,
    order_to_kalshi,
    positions_to_kalshi,
    signing_key,
    ticker_close,
)
from kalshi_moonshot.polymarket import PolymarketClient
from kalshi_moonshot.strategy import best_bid, side_ask_notional


def _gold() -> dict:
    return {
        "slug": "cpausp-gold-updown-15m-2026-10-27-1245z",
        "question": "Gold Up or Down 15m",
        "subtitle": "Oct 27, 8:30AM - 8:45AM",
        "description": "Close at or above the open.",
        "active": True,
        "closed": False,
        "status": "MARKET_STATUS_OPEN",
        "volume": 1200,
        "bestBidQuote": {"value": "0.6400", "currency": "USD"},
        "bestAskQuote": {"value": "0.6600", "currency": "USD"},
        "assetPriceTerms": {
            "marketType": "ASSET_PRICE_MARKET_TYPE_UP_DOWN",
            "asset": {"symbol": "gold"},
            "horizon": "15m",
            "windowStart": "2026-10-27T12:30:00Z",
            "windowEnd": "2026-10-27T12:45:00Z",
            "priceToBeat": {"value": "4021.95", "currency": "USD"},
        },
    }


def test_a_gold_window_uses_the_kalshi_series_and_stamp():
    row = market_to_kalshi(_gold())
    assert row is not None
    assert row["ticker"] == "KXGOLD15M-26OCT270845"
    assert row["series_ticker"] == "KXGOLD15M"
    assert row["event_ticker"] == "KXGOLD15M-26OCT270845"
    assert row["status"] == "open"
    assert row["market_type"] == "binary"
    assert row["yes_bid_dollars"] == "0.6400"
    assert row["yes_ask_dollars"] == "0.6600"
    assert row["floor_strike"] == pytest.approx(4021.95)
    assert row["strike_type"] == "greater_or_equal"
    assert row["polymarket_slug"].startswith("cpausp-gold")
    assert row["close_time"].startswith("2026-10-27T12:45:00")


def test_metadata_shape_from_the_docs_maps_the_same_way():
    raw = {
        "symbol": "cpc-btc-updown-15m-2026-09-21-0015z",
        "active": True,
        "metadata": {
            "crypto_asset_slug": "btc",
            "crypto_market_type": "updown",
            "crypto_horizon": "15m",
            "interval_end": "1789950600",
            "price_to_beat": "81650.14",
            "crypto_settlement_price": "81730.83",
        },
    }
    row = market_to_kalshi(raw)
    assert row is not None
    assert row["series_ticker"] == "KXBTC15M"
    assert row["result"] == "yes"
    assert row["polymarket_slug"] == raw["symbol"]


def test_the_ticker_stamp_is_the_close_in_new_york():
    close = ticker_close("KXGOLD15M-26OCT270845")
    assert close is not None
    assert close.isoformat().startswith("2026-10-27T08:45:00")


def test_a_restart_finds_the_slug_from_the_ticker():
    raw = _gold()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"markets": [raw]})

    client = PolymarketClient(gateway="https://gateway.test", api="https://api.test")
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    row = client.market("KXGOLD15M-26OCT270845")
    assert row["polymarket_slug"] == raw["slug"]
    assert client.event_markets("KXGOLD15M-26OCT270845")[0]["ticker"] == row["ticker"]


def test_a_sports_market_is_not_adapted():
    assert market_to_kalshi({"slug": "chiefs-super-bowl", "question": "Chiefs"}) is None


def test_the_book_is_yes_bids_and_no_bids():
    book = book_to_kalshi({"marketData": {
        "bids": [{"px": {"value": "0.42"}, "qty": "10"}],
        "offers": [{"px": {"value": "0.75"}, "qty": "4"}],
    }})
    assert best_bid(book, "yes") == pytest.approx(0.42)
    assert best_bid(book, "no") == pytest.approx(0.25)  # YES offered at 0.75
    # a YES bid at 0.42 is NO for sale at 0.58
    assert side_ask_notional(book, 0.60, "no") == pytest.approx(0.58 * 10)


def test_a_no_fill_keeps_the_no_price():
    resp = {"id": "ord-1", "executions": [{
        "type": "EXECUTION_TYPE_FILL",
        "lastShares": "4",
        "lastPx": {"value": "0.25", "currency": "USD"},
    }]}
    out = order_to_kalshi(resp, "no")
    assert out["order_id"] == "ord-1"
    assert out["fill_count"] == pytest.approx(4)
    assert out["average_fill_price"] == pytest.approx(0.25)


def test_positions_and_balance_match_the_kalshi_readers():
    body = positions_to_kalshi({
        "positions": {
            "gold-slug": {
                "netPositionDecimal": "3.37",
                "cost": {"value": "3.00", "currency": "USD"},
                "marketMetadata": {"slug": "gold-slug"},
            },
            "btc-slug": {"netPositionDecimal": "-4", "cost": {"value": "1.00", "currency": "USD"}},
        },
    }, {"gold-slug": "KXGOLD15M-26OCT270845"})
    held = parse_positions(body)
    by_ticker = {p.ticker: p for p in held}
    assert by_ticker["KXGOLD15M-26OCT270845"].side == "yes"
    assert by_ticker["KXGOLD15M-26OCT270845"].count == pytest.approx(3.37)
    assert by_ticker["btc-slug"].side == "no"
    bal = balance_to_kalshi({"balances": [{"currency": "USD", "buyingPower": 42.5, "currentBalance": 50}]})
    assert float(bal["balance_breakdown"][0]["balance"]) == pytest.approx(42.5)
    assert bal["balance_breakdown"][0]["exchange_index"] == 0


def test_the_secret_file_is_the_signing_key(tmp_path: Path):
    key = Ed25519PrivateKey.generate()
    secret = base64.b64encode(key.private_bytes_raw()).decode()
    path = tmp_path / "poly.key"
    path.write_text(secret + "\n")
    loaded = signing_key(path.read_text())
    assert loaded.public_key().public_bytes_raw() == key.public_key().public_bytes_raw()


def test_an_order_is_sent_with_the_side_price_and_comes_back_filled():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(200, json={"id": "ord-9", "executions": [{
            "type": "EXECUTION_TYPE_FILL", "lastShares": "2", "lastPx": {"value": "0.40"},
        }]})

    client = PolymarketClient(gateway="https://gateway.test", api="https://api.test")
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    client._key = Ed25519PrivateKey.generate()
    client.key_id = "key-1"
    client._slug_of["KXGOLD15M-26OCT270845"] = "gold-slug"
    out = client.buy_no_ioc("KXGOLD15M-26OCT270845", 2, 0.40)
    assert seen["path"] == "/v1/orders"
    assert b"ORDER_INTENT_BUY_SHORT" in seen["body"]
    assert b'"value":"0.4000"' in seen["body"]
    assert b"TIME_IN_FORCE_IMMEDIATE_OR_CANCEL" in seen["body"]
    assert out["fill_count"] == pytest.approx(2)
    assert out["average_fill_price"] == pytest.approx(0.40)
    assert out["order_id"] == "ord-9"
