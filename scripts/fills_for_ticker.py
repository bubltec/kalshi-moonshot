"""Read-only: the account's fills and orders for one market, with times (TICKER=... env). 2026-11-24: why the lane did not see the ZEC 12:30 fill. Only GET calls through the bot's client."""
import os
import time

from kalshi_moonshot import config
from kalshi_moonshot.kalshi import KalshiClient

T = os.environ.get("TICKER", "KXZEC15M-26NOV241230-30")
s = config.reload()
c = KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)
for f in c.portfolio_rows("/portfolio/fills", "fills", int(time.time()) - 3 * 3600, pages=3):
    if f.get("ticker") == T:
        print("FILL", {k: f.get(k) for k in ("created_time", "order_id", "side", "action", "count_fp", "yes_price_dollars", "no_price_dollars", "is_taker", "ts") if f.get(k) is not None})
body = c._request("GET", "/portfolio/orders", params={"ticker": T, "limit": 50}, auth=True)
for o in body.get("orders", []):
    print("ORDER", {k: o.get(k) for k in ("created_time", "last_update_time", "order_id", "status", "side", "action", "yes_price_dollars", "no_price_dollars", "initial_count_fp", "fill_count_fp", "remaining_count_fp", "expiration_time") if o.get(k) is not None})
