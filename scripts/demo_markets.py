"""WHICH DEMO MARKETS CAN A RESTING BUY BE TESTED ON: lists open markets on the Kalshi DEMO exchange and reads each candidate's book, to find ones where both the YES and the NO side have
a bid, a spread wide enough for a post-only order to rest under the ask, and some depth. Refuses to run unless the env file says KALSHI_ENV=demo. Places nothing.
    .venv/bin/python scripts/demo_markets.py [env_file=.env.demo] [candidates=60]
"""
import sys
from pathlib import Path

from kalshi_moonshot import config
from kalshi_moonshot.kalshi import KalshiClient, KalshiError
from kalshi_moonshot.strategy import best_ask, best_bid

env_file = Path(sys.argv[1] if len(sys.argv) > 1 else ".env.demo")
limit = int(sys.argv[2]) if len(sys.argv) > 2 else 60
s = config.reload(env_file)
if s.env != "demo" or "demo" not in s.base_url:
    sys.exit(f"refusing to run: {env_file} gives env={s.env} base={s.base_url}")
client = KalshiClient(s.base_url, s.key_id, s.private_key_path, s.max_rps, s.page_size)


def activity(m: dict) -> float:
    for k in ("volume_24h_fp", "volume_fp", "volume_24h", "volume", "open_interest_fp", "open_interest"):
        try:
            if m.get(k) is not None:
                return float(m[k])
        except (TypeError, ValueError):
            continue
    return 0.0


def depth(book: dict, side: str) -> float:
    raw = book.get("orderbook_fp") or book
    total = 0.0
    for row in raw.get("yes_dollars" if side == "yes" else "no_dollars") or []:
        try:
            total += float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
    return total


markets = list(client.open_markets(pages=3))
print(f"{len(markets)} open markets on {s.base_url}")
markets.sort(key=activity, reverse=True)
rows = []
for m in markets[:limit]:
    try:
        book = client.orderbook(m["ticker"])
    except KalshiError as exc:
        print(f"  {m['ticker']}: book unreadable ({exc})")
        continue
    ya, na, yb, nb = best_ask(book, "yes"), best_ask(book, "no"), best_bid(book, "yes"), best_bid(book, "no")
    if None in (ya, na, yb, nb):
        continue
    rows.append((m["ticker"], yb, ya, nb, na, depth(book, "yes"), depth(book, "no"), activity(m)))
# A resting buy needs room under the ask: the spread (ask - bid) must be at least 2c so a bid 1c under the ask is not the best bid already.
good = [r for r in rows if r[2] - r[1] >= 0.02 and r[4] - r[3] >= 0.02]
print(f"{len(rows)} of the {min(limit, len(markets))} most active have a bid on both sides; {len(good)} have 2c+ of room on both YES and NO\n")
print(f"{'ticker':44} {'yes bid/ask':>12} {'no bid/ask':>12} {'yes depth':>10} {'no depth':>9} {'activity':>9}")
for t, yb, ya, nb, na, dy, dn, act in sorted(good, key=lambda r: min(r[5], r[6]), reverse=True)[:25]:
    print(f"{t:44} {yb:5.2f}/{ya:5.2f} {nb:6.2f}/{na:5.2f} {dy:10.0f} {dn:9.0f} {act:9.0f}")
