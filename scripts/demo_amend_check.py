"""Prove Kalshi's amend-order call on the DEMO exchange before the bot uses it (2026-11-22, user: "you could test it against the demo environment ... once the function confirms it works you can implement it").

What it does, in order, with one contract of play money: rests a YES buy at 1c (far under the bid, so it cannot fill), amends it to 2c through
POST /portfolio/events/orders/{order_id}/amend, reads the order back, and cancels it. It refuses to run unless the configured base URL is the
demo exchange. It prints what Kalshi answered at each step and never prints a key.

    KALSHI_ENV=demo KALSHI_KEY_ID=<demo key id> KALSHI_PRIVATE_KEY_PATH=<demo private key file> .venv/bin/python scripts/demo_amend_check.py
"""

import json
import sys
import time

from kalshi_moonshot import config
from kalshi_moonshot.kalshi import KalshiClient, KalshiError

s = config.reload()
if "demo" not in s.base_url or s.env != "demo":
    sys.exit(f"refusing to run: the base URL is {s.base_url} and the env is {s.env!r}; this check only runs against the demo exchange")
if not s.key_id or not s.private_key_path:
    sys.exit("no demo credentials: set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH to a DEMO key (see the usage line above)")
c = KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)
print("exchange:", s.base_url)


def show(label, value):
    print(f"{label}: {json.dumps(value, default=str)[:600]}")


markets = c._request("GET", "/markets", params={"status": "open", "limit": 200}).get("markets", [])
pick = next((m for m in markets if float(m.get("yes_bid_dollars") or 0) >= 0.10 and float(m.get("yes_ask_dollars") or 0) < 1.0), None)
if pick is None:
    sys.exit("no open demo market with a YES bid of 10c or more; nothing to rest an order under")
ticker = pick["ticker"]
print(f"market: {ticker} (YES bid {pick['yes_bid_dollars']}, ask {pick['yes_ask_dollars']})")

order_id = None
try:
    placed = c.buy_resting(ticker, "yes", 1, 0.01, 120)
    show("1. rested a buy at 0.01", placed)
    order_id = placed.get("order_id") or (placed.get("order") or {}).get("order_id")
    if not order_id:
        sys.exit("the exchange gave no order id; stopping")
    body = {"ticker": ticker, "side": "bid", "price": "0.0200", "count": "1.00"}
    try:
        amended = c._request("POST", f"/portfolio/events/orders/{order_id}/amend", json=body, auth=True)
        show("2. amended to 0.02 (expiry omitted)", amended)
    except KalshiError as exc:
        show("2. amend REFUSED", str(exc))
        amended = None
    time.sleep(1.0)
    try:
        got = c._request("GET", f"/portfolio/events/orders/{order_id}", auth=True)
        show("3. the order afterwards", got)
    except KalshiError as exc:
        show("3. could not read the order back", str(exc))
    if amended is not None:
        try:
            c._request("POST", f"/portfolio/events/orders/{order_id}/amend", json=body, auth=True)
            show("4. the same amend again", "accepted (no error)")
        except KalshiError as exc:
            show("4. the same amend again (expect a no-op refusal)", str(exc))
finally:
    if order_id:
        try:
            show("5. cancelled", c.cancel_order(order_id))
        except KalshiError as exc:
            show("5. cancel", str(exc))
