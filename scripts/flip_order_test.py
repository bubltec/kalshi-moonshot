"""Does ONE order flip a position from YES to NO? A test on one market with one contract. Dry run unless --yes.

Kalshi's order endpoint has a single YES book: an ask sells YES, and selling YES is the same trade as buying NO at 1 - price. The docs do not say what happens when an ask is bigger than the YES you hold. This test finds out:
  1. buy 1 YES (immediate-or-cancel, a little over the ask),
  2. read the position (expect +1 YES),
  3. send ONE ask for 2 contracts, a little under the bid, NOT reduce_only,
  4. read the position again: -1 means the order sold the YES and opened a NO (a flip in one order); 0 means it sold only what we held; an error means it was refused,
  5. clean up: close whatever is left so the account is flat on that market.
It prints each step and every response. Costs: the spread twice plus two taker fees, a few cents, and at most about $1.10 is tied up while it runs.

    ENV_FILE=.env.demo python scripts/flip_order_test.py [TICKER] [--yes] [--loose]   (the demo environment: --loose accepts its thin books: put a demo API key in a separate env file, never in .env)
    python scripts/flip_order_test.py [TICKER] --yes --allow-prod                  (the real account: one contract, about a dollar)
"""
import sys
import time
from datetime import datetime
from typing import Any

from kalshi_moonshot import config
from kalshi_moonshot.kalshi import KalshiClient, KalshiError


def signed_position(client: Any, ticker: str) -> float:
    """+n for n YES contracts held, -n for n NO, 0 for none (the account's own signed count for the market)."""
    for m in client.positions().get("market_positions", []):
        if m.get("ticker") == ticker:
            return float(m.get("position_fp") or 0.0)
    return 0.0


def pick_market(client: Any, loose: bool = False) -> str | None:
    """An open BTC 15-minute market with more than 6 minutes left, a bid of at least 10c and a spread of 3c or less, the closest to even. `loose` (for the thin demo exchange, where the money is play money): 3 minutes, a bid of 5c, a spread of 12c."""
    best = None
    now = time.time()
    for m in client.series_markets("KXBTC15M"):
        if m.get("status") not in ("active", "open"):
            continue
        close = datetime.fromisoformat(m["close_time"]).timestamp()
        bid, ask = float(m.get("yes_bid_dollars") or 0), float(m.get("yes_ask_dollars") or 0)
        need, floor, spread, cap = (180, 0.05, 0.12, 0.95) if loose else (360, 0.10, 0.03, 0.90)
        if close - now > need and bid >= floor and 0 < ask - bid <= spread and ask < cap and (best is None or abs(bid - 0.5) < abs(best[0] - 0.5)):
            best = (bid, m["ticker"])
    return best[1] if best else None


def run(client: Any, ticker: str, send: bool, say=print) -> dict[str, Any]:
    m = client.market(ticker)
    bid, ask = float(m["yes_bid_dollars"]), float(m["yes_ask_dollars"])
    say(f"{ticker}: YES bid {bid:.2f} / ask {ask:.2f}; position now {signed_position(client, ticker):+g}")
    if signed_position(client, ticker) != 0:
        return {"ok": False, "why": "there is already a position on this market; the test needs a flat one"}
    idx = m.get("exchange_index")
    if idx is not None and hasattr(client, "balance"):
        try:
            rows = client.balance().get("balance_breakdown") or []
            here = {int(r.get("exchange_index", -1)): float(r.get("balance") or 0.0) for r in rows}
            say(f"exchange {idx} (where this market lives) holds ${here.get(int(idx), 0.0):.2f}; all exchanges: {here}")
            if here.get(int(idx), 0.0) < 1.5:
                other = max(here, key=lambda k: here[k])
                return {"ok": False, "why": f"this market lives on exchange {idx}, which holds ${here.get(int(idx), 0.0):.2f}. Move a few dollars there first, for example:  ENV_FILE=<your env file> python -m kalshi_moonshot shardmove --from event:{other} --to event:{idx} --usd 5 --yes"}
        except Exception as exc:  # the balance is a courtesy check, never a reason to stop
            say(f"(could not read the balance: {exc})")
    plan = f"buy 1 YES at <= {ask + 0.02:.2f}; then ONE ask for 2 at {max(0.01, bid - 0.02):.2f} (not reduce_only); then close what is left"
    say("plan: " + plan)
    if not send:
        return {"ok": True, "dry": True, "plan": plan}
    out: dict[str, Any] = {"ok": True}
    r1 = client.buy_yes_ioc(ticker, 1.0, round(ask + 0.02, 2))
    say(f"1. bought: filled {r1.get('fill_count')} at {r1.get('average_fill_price')}")
    time.sleep(1.0)
    p1 = signed_position(client, ticker)
    say(f"2. position after the buy: {p1:+g}")
    out["after_buy"] = p1
    if p1 < 0.99:
        return {**out, "ok": False, "why": "the buy did not fill; nothing more was sent"}
    body = {"ticker": ticker, "client_order_id": f"fliptest-{int(time.time())}", "side": "ask", "count": "2.00", "price": f"{max(0.01, bid - 0.02):.4f}", "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross"}
    try:
        r2 = client._request("POST", "/portfolio/events/orders", json=body, auth=True)
        say(f"3. the swap order: {r2}")
        out["swap_response"] = r2
    except KalshiError as exc:
        say(f"3. the swap order was refused: {exc}")
        out["swap_error"] = str(exc)
    time.sleep(1.0)
    p2 = signed_position(client, ticker)
    out["after_swap"] = p2
    verdict = "FLIPPED: one order sold the YES and opened a NO" if p2 < -0.99 else "SOLD ONLY WHAT WAS HELD (no NO opened)" if abs(p2) < 0.01 else "UNCHANGED or partial: " + str(p2)
    say(f"4. position after the swap order: {p2:+g}  ->  {verdict}")
    out["verdict"] = verdict
    # 5. clean up whatever is left
    if p2 < -0.01:
        mm = client.market(ticker)
        r3 = client.sell_no_ioc(ticker, abs(p2), max(0.01, 1.0 - (float(mm["yes_ask_dollars"]) + 0.02)))
        say(f"5. closed the NO: filled {r3.get('fill_count')}")
    elif p2 > 0.01:
        mm = client.market(ticker)
        r3 = client.sell_yes_ioc(ticker, p2, max(0.01, float(mm["yes_bid_dollars"]) - 0.02))
        say(f"5. closed the YES: filled {r3.get('fill_count')}")
    time.sleep(1.0)
    out["final"] = signed_position(client, ticker)
    say(f"   final position: {out['final']:+g}")
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    s = config.reload()
    send = "--yes" in sys.argv
    if s.env == "prod" and send and "--allow-prod" not in sys.argv:
        print("This is the real account. Add --allow-prod to confirm, or point ENV_FILE at a demo env file.")
        return 2
    client = KalshiClient(s.base_url, s.key_id, s.private_key_path, 1.0, s.page_size)
    print(f"environment: {s.env} ({s.base_url})")
    ticker = args[0] if args else pick_market(client, "--loose" in sys.argv)
    if not ticker:
        print("no suitable open BTC 15-minute market right now (needs 6+ minutes left, a spread of 3c or less, a bid of 10c or more; --loose relaxes that to 3 minutes, 12c and 5c)")
        return 1
    res = run(client, ticker, send)
    print(res if not res.get("ok") else "")
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
