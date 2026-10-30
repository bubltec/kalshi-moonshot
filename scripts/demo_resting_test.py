"""DOES A POST-ONLY RESTING BUY WORK ON BOTH SIDES: on the Kalshi DEMO exchange, for one market, rests a 1-contract post-only buy 1c under the ask on YES and again on NO (KalshiClient.buy_resting, the
call runner._place_resting makes), checks the order shows up resting at the price asked for, then cancels it; then sends a buy AT the ask on each side, which post-only must refuse ("post only cross").
Refuses to run unless the env file says KALSHI_ENV=demo. Costs at most one contract if a resting order happens to fill.
    .venv/bin/python scripts/demo_resting_test.py TICKER [env_file=.env.demo]
"""
import sys
import time
from pathlib import Path

from kalshi_moonshot import config
from kalshi_moonshot.kalshi import KalshiClient, KalshiError
from kalshi_moonshot.strategy import best_ask, best_bid

if len(sys.argv) < 2:
    sys.exit(__doc__)
ticker = sys.argv[1]
s = config.reload(Path(sys.argv[2] if len(sys.argv) > 2 else ".env.demo"))
if s.env != "demo" or "demo" not in s.base_url:
    sys.exit(f"refusing to run: env={s.env} base={s.base_url}")
client = KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)


def resting() -> dict[str, dict]:
    body = client._request("GET", "/portfolio/orders", params={"ticker": ticker, "status": "resting", "limit": 50}, auth=True)
    return {o.get("order_id"): o for o in body.get("orders", [])}


for side in ("yes", "no"):
    book = client.orderbook(ticker)
    ask, bid = best_ask(book, side), best_bid(book, side)
    print(f"\n== {side.upper()}  bid {bid}  ask {ask}")
    if ask is None:
        print("   no ask, skipped")
        continue
    price = round(ask - 0.01, 4)
    try:
        resp = client.buy_resting(ticker, side, 1, price, 60)
    except KalshiError as exc:
        print(f"   rest at {price:.2f}: REFUSED {exc}")
    else:
        oid = resp.get("order_id") or (resp.get("order") or {}).get("order_id") or ""
        time.sleep(1.0)
        row = resting().get(oid)
        print(f"   rest at {price:.2f}: accepted, order {oid[:8]}; on the resting list: {bool(row)}")
        if row:
            print(f"   raw side/price on the order: { {k: row.get(k) for k in ('side', 'yes_price_dollars', 'no_price_dollars', 'price', 'status', 'fill_count', 'taker_fees_dollars', 'maker_fees_dollars')} }")
        try:
            client.cancel_order(oid)
            print("   cancelled")
        except KalshiError as exc:
            print(f"   cancel failed: {exc}")
    try:
        client.buy_resting(ticker, side, 1, round(ask, 4), 60)
        print(f"   buy AT the ask {ask:.2f}: ACCEPTED (post-only did not refuse it; it may have filled as a taker)")
    except KalshiError as exc:
        print(f"   buy AT the ask {ask:.2f}: refused as it should be: {str(exc)[:140]}")
print("\nresting orders left on the market:", len(resting()))
