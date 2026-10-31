"""The lane's own last-seconds cash-out (cash_left_seconds, strategy.cash_if_losing), replayed on every live commodity bet of the last DAYS days. Read-only.

In the last WITHIN seconds of a bet the rule sells at the held bid when that bid is under the price paid and the last 3 seconds were a hover (2+ prints, strategy.price_shape == "hover"). LINE adds a cut-off the live rule does not
have: sell only while the bid is under LINE (a clear loser), and leave a close one alone; 1.0 is the live rule. The sale gets the held bid (the live rule sells at the bid it sees) less the taker fee. A bet never sold settles at 1 or 0.
"saved" = sold bets that settled as a loss, "cost" = sold bets that would have WON, "got back" = a saved bet's proceeds as a share of its stake. "vs holding" is the rule's P&L minus holding every bet to settlement.

    scripts/cloud.sh run scripts/cash_hover_replay.py [DAYS=7] [WITHIN=5]
"""
import os
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract, price_shape

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
DAYS = float(os.environ.get("DAYS", "7"))
WITHIN = float(os.environ.get("WITHIN", "5"))

bets = main.db.execute(
    "SELECT d.ticker, d.side, d.ask, d.contracts, w.close_ts, w.result FROM decisions d JOIN windows w ON w.ticker = d.ticker "
    "WHERE d.lane='commodity' AND d.action='bet' AND d.mode='trade' AND d.ts > ? AND w.result IN ('yes','no') AND d.contracts > 0 ORDER BY d.ts", (time.time() - DAYS * 86400,)).fetchall()
data = []
for b in bets:
    close = float(b["close_ts"])
    q = [(float(t), float(bid), float(ask)) for t, bid, ask in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (b["ticker"], close - 12, close + 1)).fetchall()]
    if len(q) < 5:
        continue
    pts = [(t, bid if b["side"] == "yes" else 1.0 - ask) for t, bid, ask in q]
    data.append((b, close, pts))


def base(b):
    won = b["result"] == b["side"]
    ask, n = float(b["ask"]), float(b["contracts"])
    return ((1.0 if won else 0.0) - ask - fee_per_contract(ask)) * n


def run(line):
    total = held_total = 0.0
    sold = saved = cost = 0
    got = []
    for b, close, pts in data:
        ask, n = float(b["ask"]), float(b["contracts"])
        pnl = base(b)
        for t in range(int(close - WITHIN), int(close)):
            hist = [x for x in pts if x[0] <= t]
            if not hist:
                continue
            bid = hist[-1][1]
            recent = [p for ts, p in hist if t - 3.0 <= ts <= t and p > 0]
            if bid <= 0.0 or bid >= ask or bid >= line or len(recent) < 2 or price_shape(hist, t, bid) != "hover":
                continue
            pnl = (bid - ask - fee_per_contract(ask) - fee_per_contract(bid)) * n
            sold += 1
            if b["result"] == b["side"]:
                cost += 1
            else:
                saved += 1
                got.append(bid / ask)
            break
        total += pnl
        held_total += base(b)
    return sold, saved, cost, (sum(got) / len(got) if got else 0.0), total - held_total


print(f"{len(data)} live commodity bets over {DAYS:g} days; holding every one to settlement: {sum(base(b) for b, _c, _p in data):+.2f} dollars; settled as a loss: {sum(1 for b, _c, _p in data if b['result'] != b['side'])}\n")
print(f"last {WITHIN:g} s | {'sell only if bid under':>22} | {'sold':>4} {'saved':>5} {'cost':>4} {'got back':>8} | {'vs holding':>10}")
for line in (1.0, 0.50, 0.40, 0.30, 0.20, 0.10):
    sold, saved, cost, back, vs = run(line)
    print(f"{'':>8} | {line:>22.2f}{'  (live rule)' if line == 1.0 else '':<13}| {sold:>4} {saved:>5} {cost:>4} {back:>7.0%} | {vs:>+10.2f}")
print("DONE")
