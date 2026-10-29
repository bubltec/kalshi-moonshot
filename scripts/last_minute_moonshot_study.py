"""Does the cheap side of a 15-minute market in its last minute pay more than its price? The NATGAS finish: YES at 3-10c with 40 seconds left, settled YES. Replayed from the tape.

For every settled 15-minute market on the tape (any series), LEFT seconds before the close the cheap side is the side whose ask is CHEAP (0.10) or less. Two ways to hold a $1-payout ticket on it:

  taker     buy one contract at the ask at that moment and pay the taker fee
  resting   rest a buy at BID dollars (below the ask) and keep it until the close. It counts as filled when, in a later second, takers sold into that price: for a YES
            ticket a second with NO-taker volume at an average YES price of BID or less, for a NO ticket a second with YES-taker volume at an average YES price of 1 - BID or
            more. A resting fill pays no fee. The per-second average is a loose test of a fill: a real fill is somewhat rarer than this says.

A ticket wins if its side settles in its favor and pays $1. P&L is per ticket in cents (win: 100c minus the price; lose: minus the price). If the tickets win more often than
their price, there is an edge to size up; if they win about as often as the price says, the market priced the finish correctly and a stake only buys lottery variance.

    scripts/cloud.sh run scripts/last_minute_moonshot_study.py [DAYS=3] [CHEAP=0.10] [FAMILY=all|coins|metals|fx|energy]
"""
import os
import re
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

DAYS = float(os.environ.get("DAYS", "3"))
CHEAP = float(os.environ.get("CHEAP", "0.10"))
FAMILY = os.environ.get("FAMILY", "all")
LEFTS = (90, 60, 30)
BIDS = (0.02, 0.03, 0.05)
METALS = {"GOLD", "SILVER", "PLATINUM", "PALLADIUM", "COPPER"}
COINS = {"BTC", "ETH", "SOL", "XRP", "DOGE"}
ENERGY = {"WTI", "BRENT", "NATGAS"}


def family(series):
    m = re.match(r"^KX([A-Z]+)15M$", series)
    n = m.group(1) if m else ""
    return "metals" if n in METALS else "coins" if n in COINS else "energy" if n in ENERGY else "fx" if len(n) == 6 else "other"


since = time.time() - DAYS * 86400
wrows = main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts >= ? ORDER BY close_ts", (since,)).fetchall()

# results[left][label] = list of (pnl in cents, won)
res = {left: {"taker": [], **{f"rest {int(b * 100)}c": [] for b in BIDS}} for left in LEFTS}
cand = {left: 0 for left in LEFTS}
markets = 0
for w in wrows:
    fam = family(w["series"])
    if FAMILY != "all" and fam != FAMILY:
        continue
    close = float(w["close_ts"])
    quotes = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 130, close)).fetchall()
    if len(quotes) < 10:
        continue
    flow = tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (w["ticker"], int(close - 130), int(close))).fetchall()
    markets += 1
    for left in LEFTS:
        t0 = close - left
        at = [q for q in quotes if t0 - 5 <= q[0] <= t0]
        if not at:
            continue
        yb, ya = float(at[-1][1]), float(at[-1][2])
        if ya <= CHEAP:
            side, ask = "yes", ya
        elif 1.0 - yb <= CHEAP:
            side, ask = "no", 1.0 - yb
        else:
            continue
        cand[left] += 1
        won = w["result"] == side
        res[left]["taker"].append((((100.0 if won else 0.0) - ask * 100.0 - fee_per_contract(ask) * 100.0), won))
        for b in BIDS:
            if b >= ask:
                continue  # a bid at or above the ask would be a taker buy
            filled = False
            for t, yv, nv, vwap in flow:
                if vwap is None or t <= t0:
                    continue
                if side == "yes" and nv and float(nv) > 0 and float(vwap) <= b + 1e-9:
                    filled = True
                    break
                if side == "no" and yv and float(yv) > 0 and float(vwap) >= 1.0 - b - 1e-9:
                    filled = True
                    break
            if filled:
                res[left][f"rest {int(b * 100)}c"].append((((100.0 if won else 0.0) - b * 100.0), won))

print(f"{markets} settled markets ({FAMILY}) over {DAYS:g} days; cheap side = ask <= {CHEAP:.2f}; P&L per ticket, cents, taker fee included for taker buys")
print(f"{'left':>5} {'ticket':<10} {'tickets':>7} {'wins':>5} {'win%':>6} {'avg price':>9} {'P&L/ticket':>11} {'ROI':>7}", flush=True)
for left in LEFTS:
    for label, rows in res[left].items():
        if not rows:
            print(f"{left:>4}s {label:<10} {0:>7}")
            continue
        n = len(rows)
        wins = sum(1 for _p, w in rows if w)
        mean = sum(p for p, _w in rows) / n
        price = {"taker": None}.get(label)
        cost = sum((100.0 - p) if w else -p for p, w in rows) / n  # average price paid, cents
        print(f"{left:>4}s {label:<10} {n:>7} {wins:>5} {wins / n:>6.1%} {cost:>8.1f}c {mean:>+10.2f}c {mean / cost:>+7.0%}")
