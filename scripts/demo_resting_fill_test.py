"""DOES A FILLED RESTING BUY PAY A FEE: on the Kalshi DEMO exchange, account 1 rests a 1-contract post-only buy one cent under the ask (KalshiClient.buy_resting, as runner._place_resting does) and
account 2 (KALSHI_KEY_ID_2 / KALSHI_PRIVATE_KEY_PATH_2 in the env file) takes it with an immediate order on the opposite contract. Both accounts' fills are then printed with their fee fields, for YES then NO.
Two accounts because one account's own taker order would be stopped by self-trade prevention. Refuses to run unless the env file says KALSHI_ENV=demo. Costs about a dollar of demo money.
    .venv/bin/python scripts/demo_resting_fill_test.py TICKER [env_file=.env.demo]
"""
import os
import sys
import time
from pathlib import Path

from kalshi_moonshot import config
from kalshi_moonshot.kalshi import KalshiClient, KalshiError
from kalshi_moonshot.strategy import best_ask

if len(sys.argv) < 2:
    sys.exit(__doc__)
ticker = sys.argv[1]
s = config.reload(Path(sys.argv[2] if len(sys.argv) > 2 else ".env.demo"))
if s.env != "demo" or "demo" not in s.base_url:
    sys.exit(f"refusing to run: env={s.env} base={s.base_url}")
key2, path2 = os.environ.get("KALSHI_KEY_ID_2", ""), os.environ.get("KALSHI_PRIVATE_KEY_PATH_2", "")
if not key2 or not path2 or key2 == s.key_id:
    sys.exit("needs KALSHI_KEY_ID_2 and KALSHI_PRIVATE_KEY_PATH_2 for a second account, different from the first")
maker = KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)
taker = KalshiClient(s.base_url, key2, path2, s.max_rps, s.page_size)
print("balances: maker", maker.balance().get("balance_dollars"), " taker", taker.balance().get("balance_dollars"))
started = int(time.time()) - 5


def show(label: str, client: KalshiClient, order_id: str) -> None:
    rows = [f for f in client.portfolio_rows("/portfolio/fills", "fills", started, pages=1) if f.get("order_id") == order_id]
    if not rows:
        print(f"   {label}: no fill found for order {order_id[:8]}")
    for f in rows:
        print(f"   {label}: " + ", ".join(f"{k}={v}" for k, v in sorted(f.items()) if any(w in k for w in ("fee", "taker", "side", "count", "price", "action"))))


for side in ("yes", "no"):
    book = maker.orderbook(ticker)
    ask = best_ask(book, side)
    print(f"\n== resting {side.upper()} buy, ask {ask}")
    if ask is None:
        continue
    price = round(ask - 0.01, 4)
    try:
        resp = maker.buy_resting(ticker, side, 1, price, 60)
    except KalshiError as exc:
        print(f"   rest refused: {exc}")
        continue
    oid = resp.get("order_id") or (resp.get("order") or {}).get("order_id") or ""
    print(f"   rested at {price:.2f}, order {oid[:8]}")
    time.sleep(1.0)
    other = round(1.0 - price, 4)  # the opposite contract at the price that meets it
    try:
        t = taker.buy_no_ioc(ticker, 1, other) if side == "yes" else taker.buy_yes_ioc(ticker, 1, other)
    except KalshiError as exc:
        print(f"   taker order refused: {exc}")
        t = {}
    toid = t.get("order_id") or (t.get("order") or {}).get("order_id") or ""
    print(f"   taker bought the {'NO' if side == 'yes' else 'YES'} side at up to {other:.2f}: fill_count {t.get('fill_count')}, order {toid[:8]}")
    time.sleep(2.0)
    show("maker fill", maker, oid)
    if toid:
        show("taker fill", taker, toid)
    try:
        maker.cancel_order(oid)
    except KalshiError:
        pass  # a filled order is already gone
