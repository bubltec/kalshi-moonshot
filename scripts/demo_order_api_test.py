"""Everything we have not confirmed about Kalshi's order API, on the DEMO exchange only (play money; it refuses to run on a real account). One market, one or two contracts at a time.

    ENV_FILE=.env.demo python scripts/demo_order_api_test.py TICKER

Tests, each printed with the raw response:
  A  a resting bid far under the market: it rests, its queue position, an amend (price), a cancel
  B  the flip the other way: buy 1 NO, then ONE bid for 2 (not reduce_only): does it end as 1 YES?
  C  reduce_only: hold 1 YES, send an ask for 2 with reduce_only: does it sell only 1?
  D  the fee on a resting order: a bid just above the best bid, left for 60 seconds (a counterparty may or may not come)
It ends flat on the market (it closes whatever is left).
"""
import sys
import time
import uuid

from kalshi_moonshot import config
from kalshi_moonshot.kalshi import KalshiClient, KalshiError

s = config.reload()
if s.env != "demo":
    sys.exit("This test places real orders. It runs only against the demo exchange: point ENV_FILE at your demo env file.")
c = KalshiClient(s.base_url, s.key_id, s.private_key_path, 1.5, s.page_size)
tk = sys.argv[1]


def pos() -> float:
    for m in c.positions().get("market_positions", []):
        if m.get("ticker") == tk:
            return float(m.get("position_fp") or 0.0)
    return 0.0


def book() -> tuple[float, float]:
    m = c.market(tk)
    return float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"])


def order(side, count, price, tif="immediate_or_cancel", **extra):
    body = {"ticker": tk, "client_order_id": str(uuid.uuid4()), "side": side, "count": f"{count:.2f}", "price": f"{price:.4f}", "time_in_force": tif, "self_trade_prevention_type": "taker_at_cross", **extra}
    return c._request("POST", "/portfolio/events/orders", json=body, auth=True)


def flatten():
    p = pos()
    if p > 0.01:
        print("  flatten: sell", p, "YES ->", order("ask", p, 0.02, reduce_only=True).get("fill_count"))
    elif p < -0.01:
        print("  flatten: buy back", -p, "->", order("bid", -p, 0.98, reduce_only=True).get("fill_count"))


print("environment:", s.env, "| market", tk, "| book", book(), "| position", pos())
flatten()

print("\nA. a resting order: place, queue position, amend, cancel")
bid, ask = book()
low = max(0.02, round(bid - 0.20, 2))
try:
    r = order("bid", 1, low, tif="good_till_canceled", post_only=True)
    oid = r["order_id"]
    print("  placed:", r)
    try:
        print("  queue position:", c._request("GET", f"/portfolio/orders/{oid}/queue_position", auth=True))
    except KalshiError as e:
        print("  queue position error:", str(e)[:160])
    try:
        print("  amended to", round(low + 0.02, 2), ":", c._request("POST", f"/portfolio/events/orders/{oid}/amend", json={"ticker": tk, "side": "bid", "price": f"{low + 0.02:.4f}", "count": "1.00"}, auth=True))
    except KalshiError as e:
        print("  amend error:", str(e)[:200])
    try:
        print("  cancelled:", c._request("DELETE", f"/portfolio/events/orders/{oid}", params={"market_ticker": tk}, auth=True))
    except KalshiError as e:
        print("  cancel error:", str(e)[:200])
except KalshiError as e:
    print("  resting order error:", str(e)[:200])

print("\nB. flip the other way: buy 1 NO (an ask of 1 on the YES book), then ONE bid for 2")
bid, ask = book()
r = order("ask", 1, max(0.01, bid - 0.03))
print("  bought NO:", {k: r.get(k) for k in ("fill_count", "average_fill_price", "average_fee_paid")}, "-> position", pos())
bid, ask = book()
r = order("bid", 2, min(0.99, ask + 0.03))
print("  ONE bid for 2:", {k: r.get(k) for k in ("fill_count", "average_fill_price", "average_fee_paid")}, "-> position", pos(), "(+1 means it bought back the NO and opened a YES)")
flatten()

print("\nC. reduce_only: hold 1 YES, ask for 2 with reduce_only")
bid, ask = book()
r = order("bid", 1, min(0.99, ask + 0.03))
print("  bought 1 YES -> position", pos())
bid, ask = book()
try:
    r = order("ask", 2, max(0.01, bid - 0.03), reduce_only=True)
    print("  ask for 2 reduce_only:", {k: r.get(k) for k in ("fill_count", "remaining_count")}, "-> position", pos(), "(0 means it sold only the 1 held)")
except KalshiError as e:
    print("  reduce_only ask refused:", str(e)[:200], "-> position", pos())
flatten()

print("\nD. a resting bid just above the best bid for 60 seconds")
bid, ask = book()
price = round(min(bid + 0.01, ask - 0.01), 2)
try:
    r = order("bid", 1, price, tif="good_till_canceled", post_only=True)
    oid = r["order_id"]
    print("  resting at", price, "on a book of", (bid, ask), r)
    time.sleep(60)
    print("  position after 60 s:", pos())
    try:
        print("  cancelled:", c._request("DELETE", f"/portfolio/events/orders/{oid}", params={"market_ticker": tk}, auth=True))
    except KalshiError as e:
        print("  cancel:", str(e)[:160])
except KalshiError as e:
    print("  resting order error:", str(e)[:200])
flatten()
print("\nfinal position:", pos())
