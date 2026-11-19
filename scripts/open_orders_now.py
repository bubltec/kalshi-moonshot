"""Read-only: the account's resting orders and open positions right now. (2026-11-19, user: "you can obviously use the api to see anything in my account")
Only GET calls through the bot's own client on the host; nothing is placed, changed or cancelled, and no key is printed.
    scripts/cloud.sh run scripts/open_orders_now.py"""
import time

from kalshi_moonshot import config
from kalshi_moonshot.kalshi import KalshiClient

s = config.reload()
c = KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)
body = c._request("GET", "/portfolio/orders", params={"status": "resting", "limit": 200}, auth=True)
orders = body.get("orders", [])
print(f"resting orders: {len(orders)}")
for o in orders:
    price = o.get("yes_price_dollars") or o.get("yes_price") or o.get("no_price_dollars") or o.get("no_price")
    print(" ", o.get("ticker"), "| side", o.get("side"), "| action", o.get("action"), "| yes price", o.get("yes_price_dollars", o.get("yes_price")), "| no price", o.get("no_price_dollars", o.get("no_price")),
          "| remaining", o.get("remaining_count_fp", o.get("remaining_count")), "| created", o.get("created_time"), "| id", str(o.get("order_id"))[:8])
pos = c.positions()
rows = pos.get("market_positions", [])
print(f"open positions: {len([r for r in rows if float(r.get('position_fp', r.get('position', 0)) or 0) != 0])}")
for r in rows:
    p = float(r.get("position_fp", r.get("position", 0)) or 0)
    if p:
        print(" ", r.get("ticker"), "| position", p, "| exposure", r.get("market_exposure_dollars", r.get("market_exposure")))
print("read at", time.strftime("%H:%M:%S", time.gmtime(time.time() - 5 * 3600)), "ET")
