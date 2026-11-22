"""Prove Kalshi's amend-order call on the DEMO exchange before the bot uses it (2026-11-22, user: "you could test it against the demo environment ... once the function confirms it works you can implement it").

What it does, in order, with one contract of play money: rests a YES buy at 1c (far under the bid, so it cannot fill), amends it to 2c through
POST /portfolio/events/orders/{order_id}/amend, reads the order back, and cancels it. It refuses to run unless the configured base URL is the
demo exchange. It prints what Kalshi answered at each step and never prints a key.

    ENV_FILE=.env.demo .venv/bin/python scripts/demo_amend_check.py

(.env.demo holds the demo key id and key file. The settings loader overlays ENV_FILE, which defaults to .env, the prod file.)
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


markets: list[dict] = []
cursor = ""
for _ in range(5):  # a few pages: the demo exchange is thin and its first page may hold no quoted market
    page = c._request("GET", "/markets", params={"status": "open", "limit": 1000, **({"cursor": cursor} if cursor else {})})
    markets += page.get("markets", [])
    cursor = page.get("cursor") or ""
    if not cursor:
        break
print(f"{len(markets)} open demo markets read")


def num(m, key):
    try:
        return float(m.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


# A post-only YES buy at 0.01 and then 0.02 can never cross unless the YES ask is at 3c or under. A market with no ask at all is fine too.
pick = next((m for m in markets if m.get("market_type", "binary") == "binary" and (num(m, "yes_ask_dollars") == 0 or num(m, "yes_ask_dollars") > 0.05)), None)
if pick is None:
    sample = {k: markets[0].get(k) for k in ("ticker", "status", "yes_bid_dollars", "yes_ask_dollars", "market_type")} if markets else None
    sys.exit(f"no usable open demo market; the first one read was {sample}")
ticker = pick["ticker"]
print(f"market: {ticker} (YES bid {pick.get('yes_bid_dollars')}, ask {pick.get('yes_ask_dollars')})")

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
