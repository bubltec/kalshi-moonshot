"""Trends in the coin race from the saved history (scripts/coinrace_history.py). Read-only.

For each coin and minute into the race: the average ask, the spread, and how often it won; the cost of buying each coin at the ask at minute M and holding (with the taker fee) and of resting a bid at the bid; and whether the leader at minute M wins.
    docker compose -p moonshot-local --env-file .env.local exec -T bot python - < scripts/coinrace_trends.py
"""
import json
import math
import statistics
from collections import defaultdict

from kalshi_moonshot.strategy import fee_per_contract

COINS = ("BTC", "ETH", "SOL", "XRP", "HYPE")
races = [json.loads(x) for x in open("/data/coinrace_history.jsonl")]
races = [r for r in races if r["winner"]]
print(len(races), "races")


def at(m, close, minute):
    """(bid, ask) of the candle that ends `minute` minutes into the race (15-minute window ending at close)."""
    t = close - 900 + minute * 60
    best = None
    for c in m["candles"]:
        if c[0] <= t:
            best = c
    return None if best is None or best[2] <= 0 or best[2] >= 1.0 else (best[1], best[2])


def se(xs):
    n = len(xs)
    return (sum((x - sum(xs) / n) ** 2 for x in xs) / n) ** 0.5 / math.sqrt(n) * 100 if n > 1 else 0.0


print("\nspread and price by minute (average over coins):")
for minute in (1, 3, 5, 8, 11, 13):
    sp = [a - b for r in races for m in r["markets"].values() if (q := at(m, r["close"], minute)) for b, a in [q]]
    print(f"  minute {minute:>2}: avg spread {statistics.mean(sp) * 100:4.1f}c  median {statistics.median(sp) * 100:4.1f}c  ({len(sp)} quotes)")

print("\nbuy a coin at the ask at minute M and hold (after the taker fee), per contract:")
print("  min  " + "  ".join(f"{c:>16}" for c in COINS))
for minute in (1, 3, 5, 8, 11):
    cells = []
    for c in COINS:
        pnl = []
        for r in races:
            q = at(r["markets"][c], r["close"], minute)
            if q:
                pnl.append((1.0 if r["winner"] == c else 0.0) - q[1] - fee_per_contract(q[1]))
        cells.append(f"{statistics.mean(pnl) * 100:>+6.1f}c +-{se(pnl):4.1f}" if pnl else f"{'-':>16}")
    print(f"  {minute:>3}  " + "  ".join(f"{x:>16}" for x in cells))

print("\nrest a bid at the bid (maker, no fee) -- if it filled (an upper bound: the ask would have to come down to it); per contract:")
for minute in (1, 3, 5, 8):
    cells = []
    for c in COINS:
        pnl = []
        for r in races:
            q = at(r["markets"][c], r["close"], minute)
            if q and q[0] > 0:
                pnl.append((1.0 if r["winner"] == c else 0.0) - q[0])
        cells.append(f"{statistics.mean(pnl) * 100:>+6.1f}c +-{se(pnl):4.1f}" if pnl else f"{'-':>16}")
    print(f"  {minute:>3}  " + "  ".join(f"{x:>16}" for x in cells))

print("\ndoes the leader (highest mid) at minute M win? and what did its ask cost:")
for minute in (3, 5, 8, 11, 13):
    wins, asks, pnl = 0, [], []
    for r in races:
        qs = {c: at(r["markets"][c], r["close"], minute) for c in COINS}
        qs = {c: q for c, q in qs.items() if q}
        if len(qs) < 5:
            continue
        lead = max(qs, key=lambda c: (qs[c][0] + qs[c][1]) / 2)
        w = r["winner"] == lead
        wins += w
        asks.append(qs[lead][1])
        pnl.append((1.0 if w else 0.0) - qs[lead][1] - fee_per_contract(qs[lead][1]))
    n = len(asks)
    print(f"  minute {minute:>2}: leader wins {100 * wins / n:4.0f}% of {n}, avg ask {statistics.mean(asks):.2f}, buy at ask {statistics.mean(pnl) * 100:>+5.1f}c +-{se(pnl):.1f}")

print("\ncalibration at minute 5: ask bucket -> how often it won")
b = defaultdict(list)
for r in races:
    for c in COINS:
        q = at(r["markets"][c], r["close"], 5)
        if q:
            b[min(int(q[1] * 10), 9)].append(1.0 if r["winner"] == c else 0.0)
for k in sorted(b):
    print(f"  ask {k * 10:>2}-{k * 10 + 9:>2}c: won {100 * statistics.mean(b[k]):>5.1f}% of {len(b[k]):>4} (price says about {k * 10 + 5}%)")
print("DONE")
