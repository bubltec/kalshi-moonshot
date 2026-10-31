"""A last-seconds cash-out that sells only a bet that is going to lose, not one oscillating around 50c. Replay on every live commodity bet of the last DAYS days. Read-only.

A bet is "going to lose" at second t, with `left` seconds to the close, when its held bid has been UNDER LINE for the whole of the last STAY seconds (no bounce back over it, so a price swinging around 50c does not count) and
the other side is not merely wobbling: the best held bid of the last STAY seconds is under LINE too. The rule SELLS at the held bid LAT seconds later (less the taker fee) once that is true and `left` <= WITHIN. A bet never sold
settles at 1 or 0. "saved" = sold bets that settled as a loss, "cost" = sold bets that would have WON, "got back" = the average dollars a saved bet's sale recovered per dollar staked. "vs holding" is the rule's P&L minus holding.
The exits the lane already runs are not modeled.

    scripts/cloud.sh run scripts/cash_left_replay2.py [DAYS=7] [LAT=1]
"""
import itertools
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
bets = main.db.execute(
    "SELECT d.ticker, d.side, d.ask, d.contracts, w.close_ts, w.result FROM decisions d JOIN windows w ON w.ticker = d.ticker "
    "WHERE d.lane='commodity' AND d.action='bet' AND d.mode='trade' AND d.ts > ? AND w.result IN ('yes','no') AND d.contracts > 0 ORDER BY d.ts", (time.time() - DAYS * 86400,)).fetchall()
data = []
for b in bets:
    close = float(b["close_ts"])
    q = [(float(t), float(bid), float(ask)) for t, bid, ask in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (b["ticker"], close - 60, close + 1)).fetchall()]
    if len(q) < 5:
        continue
    held = lambda t, q=q, side=b["side"]: next(((x[1] if side == "yes" else 1.0 - x[2]) for x in reversed(q) if x[0] <= t), None)  # noqa: E731
    data.append((b, close, held))


def base(b):
    won = b["result"] == b["side"]
    ask, n = float(b["ask"]), float(b["contracts"])
    return ((1.0 if won else 0.0) - ask - fee_per_contract(ask)) * n


def run(within, line, stay):
    total = held_total = 0.0
    sold = saved = cost = 0
    got = []
    for b, close, held in data:
        ask, n = float(b["ask"]), float(b["contracts"])
        pnl = base(b)
        for t in range(int(close - within), int(close)):
            hist = [held(t - k) for k in range(stay + 1)]
            if any(x is None for x in hist) or max(hist) >= line or hist[0] <= 0.0:
                continue
            fill = held(t + LAT)
            if fill is not None and fill > 0.0:
                pnl = (fill - ask - fee_per_contract(ask) - fee_per_contract(fill)) * n
                sold += 1
                if b["result"] == b["side"]:
                    cost += 1
                else:
                    saved += 1
                    got.append(fill * n / (ask * n))
            break
        total += pnl
        held_total += base(b)
    return sold, saved, cost, (sum(got) / len(got) if got else 0.0), total - held_total


print(f"{len(data)} live commodity bets over {DAYS:g} days; holding every one to settlement: {sum(base(b) for b, _c, _h in data):+.2f} dollars; settled as a loss: {sum(1 for b, _c, _h in data if b['result'] != b['side'])}\n")
print(f"{'within':>6} {'under':>5} {'stay':>5} | {'sold':>4} {'saved':>5} {'cost':>4} {'got back':>8} | {'vs holding':>10}")
for within, line, stay in itertools.product((15, 30, 60), (0.30, 0.40, 0.50), (5, 10, 20)):
    sold, saved, cost, back, vs = run(within, line, stay)
    print(f"{within:>5}s {line:>5.2f} {stay:>4}s | {sold:>4} {saved:>5} {cost:>4} {back:>7.0%} | {vs:>+10.2f}")
print("DONE")
