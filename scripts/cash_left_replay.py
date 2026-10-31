"""Replay a last-seconds cash-out on every live commodity bet of the last DAYS days. Read-only.

In the last WITHIN seconds of a bet, the first second its held bid is under LINE (the other side is now the favorite: under 0.50 is "we are going to lose") the rule SELLS at the held bid LAT seconds later, less the taker fee
on the sale. A bid at or over LINE is left to settle. A bet never sold settles at 1 or 0. Unlike the lane's cash_left_seconds, this does not need the bid to be hovering and does not compare with the price paid. "vs holding" is the rule's total P&L minus holding every bet to settlement; "saved" counts sold bets that
settled as a loss (a total loss cut), "cost" counts sold bets that would have WON (a winner given up). The exits the lane already runs (burst, drop) are not modeled, so this is the rule on top of holding.

    scripts/cloud.sh run scripts/cash_left_replay.py [DAYS=7] [LAT=1]
"""
import os
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

DAYS = float(os.environ.get("DAYS", "7"))
LAT = int(os.environ.get("LAT", "1"))
WITHINS = (3, 5, 8, 12)
LINES = (0.30, 0.40, 0.50, 0.60)

bets = main.db.execute(
    "SELECT d.ticker, d.side, d.ask, d.contracts, w.close_ts, w.result FROM decisions d JOIN windows w ON w.ticker = d.ticker "
    "WHERE d.lane='commodity' AND d.action='bet' AND d.mode='trade' AND d.ts > ? AND w.result IN ('yes','no') AND d.contracts > 0 ORDER BY d.ts", (time.time() - DAYS * 86400,)).fetchall()
data = []
for b in bets:
    close = float(b["close_ts"])
    q = [(float(t), float(bid), float(ask)) for t, bid, ask in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (b["ticker"], close - 20, close + 1)).fetchall()]
    if len(q) < 5:
        continue
    held = lambda t, q=q, side=b["side"]: next(((x[1] if side == "yes" else 1.0 - x[2]) for x in reversed(q) if x[0] <= t), None)  # noqa: E731  (q and side bound now, not at the last loop turn)
    data.append((b, close, held))


def base(b):
    won = b["result"] == b["side"]
    ask, n = float(b["ask"]), float(b["contracts"])
    return ((1.0 if won else 0.0) - ask - fee_per_contract(ask)) * n


def run(within, line):
    total = held_total = 0.0
    sold = saved = cost = 0
    for b, close, held in data:
        ask, n = float(b["ask"]), float(b["contracts"])
        pnl = base(b)
        for t in range(int(close - within), int(close)):
            bid = held(t)
            if bid is not None and 0.0 < bid < line:
                fill = held(t + LAT)
                if fill is not None and fill > 0.0:
                    pnl = (fill - ask - fee_per_contract(ask) - fee_per_contract(fill)) * n
                    sold += 1
                    if b["result"] == b["side"]:
                        cost += 1
                    else:
                        saved += 1
                break
        total += pnl
        held_total += base(b)
    return sold, saved, cost, total - held_total


print(f"{len(data)} live commodity bets over {DAYS:g} days; holding every one to settlement: {sum(base(b) for b, _c, _h in data):+.2f} dollars; settled as a loss: {sum(1 for b, _c, _h in data if b['result'] != b['side'])}\n")
print(f"{'left':>5} {'bid under':>9} | {'sold':>4} {'saved':>5} {'cost':>4} | {'vs holding':>10}")
for w in WITHINS:
    for m in LINES:
        sold, saved, cost, vs = run(w, m)
        print(f"{w:>4}s {m:>9.2f} | {sold:>4} {saved:>5} {cost:>4} | {vs:>+10.2f}")
print("DONE")
