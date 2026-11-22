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
# The demo account is funded on some exchange indexes (shards) only, and an order on any other is refused with insufficient_shard_balance (a 404), so the check tries
# candidates until one accepts the order: the bot's own 15-minute series first, then any market that is not a multivariate combination (KXMVE...), then the rest.
def usable(m):
    return m.get("market_type", "binary") == "binary" and (num(m, "yes_ask_dollars") == 0 or num(m, "yes_ask_dollars") > 0.05)


def rank(m):
    tk = m["ticker"]
    return 0 if "15M-" in tk else (2 if tk.startswith("KXMVE") else 1)


candidates = sorted((m for m in markets if usable(m)), key=rank)
if not candidates:
    sample = {k: markets[0].get(k) for k in ("ticker", "status", "yes_bid_dollars", "yes_ask_dollars", "market_type")} if markets else None
    sys.exit(f"no usable open demo market; the first one read was {sample}")

order_id = None
ticker = ""
placed: dict = {}
for pick in candidates[:25]:
    ticker = pick["ticker"]
    try:
        placed = c.buy_resting(ticker, "yes", 1, 0.01, 120)
    except KalshiError as exc:
        if "insufficient_shard_balance" in str(exc):
            print(f"skipped {ticker}: no balance on its exchange index")
            continue
        raise
    print(f"market: {ticker} (YES bid {pick.get('yes_bid_dollars')}, ask {pick.get('yes_ask_dollars')})")
    break
else:
    sys.exit("none of the first 25 demo markets is on an exchange index this account has a balance on (fund it with POST /portfolio/intra_exchange_instance_transfer, or tell me which series to use)")

try:
    show("1. rested a buy at 0.01", placed)
    order_id = placed.get("order_id") or (placed.get("order") or {}).get("order_id")
    if not order_id:
        sys.exit("the exchange gave no order id; stopping")
    try:
        amended = c.amend_resting(order_id, ticker, "yes", "buy", 0.02, 1.0)  # the bot's own call, not a hand-made request
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
            c.amend_resting(order_id, ticker, "yes", "buy", 0.02, 1.0)
            show("4. the same amend again", "accepted (no error)")
        except KalshiError as exc:
            show("4. the same amend again (expect a no-op refusal)", str(exc))
finally:
    if order_id:
        try:
            show("5. cancelled", c.cancel_order(order_id))
        except KalshiError as exc:
            show("5. cancel", str(exc))
