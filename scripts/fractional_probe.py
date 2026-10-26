"""Find out whether Kalshi accepts a fractional contract count on a 15-minute market: one small immediate-or-cancel buy.

Dry run by default: it picks a cheap side of an open KXBTC15M market and prints the order it would send. Nothing is sent without --apply, and the order is refused
if it could cost more than --max-usd ($1 by default). A rejection is the answer "whole contracts only"; a fill with a fractional fill_count is the answer "fractional works".

    uv run python scripts/fractional_probe.py            # show the order
    uv run python scripts/fractional_probe.py --apply    # send it (real money, at most --max-usd)
"""

from __future__ import annotations

import argparse
import json

from kalshi_moonshot import config
from kalshi_moonshot.__main__ import _client
from kalshi_moonshot.strategy import flip

p = argparse.ArgumentParser()
p.add_argument("--series", default="KXBTC15M")
p.add_argument("--ticker", default="")
p.add_argument("--side", choices=["yes", "no"], default="")
p.add_argument("--count", type=float, default=1.5)
p.add_argument("--max-usd", type=float, default=1.0)
p.add_argument("--apply", action="store_true")
a = p.parse_args()

s = config.reload()
client = _client(s)
markets = [client.market(a.ticker)] if a.ticker else client.series_markets(a.series)
best = None
for m in markets:
    for side in ([a.side] if a.side else ["yes", "no"]):
        view = flip(m) if side == "no" else m
        ask = float(view.get("yes_ask_dollars") or 0)
        if 0.05 <= ask <= 0.60 and (best is None or ask > best[2]):
            best = (m["ticker"], side, ask)  # the dearest side under 60c: the most likely to fill
if best is None:
    raise SystemExit("no side priced between 5c and 60c right now; try again in a minute or pass --ticker and --side")
ticker, side, ask = best
cost = round(a.count * ask, 4)
print(f"order: buy {a.count:.2f} {side.upper()} on {ticker} at {ask:.2f} (immediate-or-cancel), at most ${cost:.2f} before the fee")
if cost > a.max_usd:
    raise SystemExit(f"refused: ${cost:.2f} is over --max-usd {a.max_usd:.2f}")
if not a.apply:
    raise SystemExit("dry run: nothing sent. Add --apply to send it.")
buy = client.buy_yes_ioc if side == "yes" else client.buy_no_ioc
try:
    resp = buy(ticker, a.count, ask)  # type: ignore[arg-type]
except Exception as exc:
    print(f"REJECTED: {exc}")
    raise SystemExit(1) from exc
print(json.dumps(resp, indent=2))
print(f"fill_count = {resp.get('fill_count')}")
