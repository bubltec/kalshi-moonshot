"""Replay a one-time hedge on every live commodity bet of the last DAYS days. Read-only.

After entry, the first second the held side's bid falls to TRIG or below (the market has gone against us, neck and neck at about 0.50) the rule buys the OTHER side at its ask (1 - held bid, plus the taker
fee), spending SHARE of the original stake in dollars. Both legs then settle: the held side pays 1 if it wins, the hedge pays 1 if the other side wins. "vs holding" is the rule's total P&L minus holding every bet
unhedged, so a positive number is money the hedge made. Rows are by TRIG and SHARE, then the bets the hedge touched are split by whether the held side went on to win.

    scripts/cloud.sh run scripts/hedge_replay.py [DAYS=7] [LAT=1]
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
MIN_LEFT = 10
TRIGS = (0.35, 0.45, 0.50, 0.55, 0.60)
SHARES = (0.25, 0.5, 1.0)

bets = main.db.execute(
    "SELECT d.ticker, d.ts, d.side, d.ask, d.contracts, d.stake_usd, w.close_ts, w.result FROM decisions d JOIN windows w ON w.ticker = d.ticker "
    "WHERE d.lane='commodity' AND d.action='bet' AND d.mode='trade' AND d.ts > ? AND w.result IN ('yes','no') AND d.contracts > 0 ORDER BY d.ts", (time.time() - DAYS * 86400,)).fetchall()
data = []
for b in bets:
    close = float(b["close_ts"])
    quotes = [(float(t), float(bid), float(ask)) for t, bid, ask in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (b["ticker"], float(b["ts"]), close)).fetchall()]
    if len(quotes) < 5:
        continue
    held = [(t, (bid if b["side"] == "yes" else 1.0 - ask), ((1.0 - bid) if b["side"] == "yes" else ask)) for t, bid, ask in quotes]  # (ts, held bid, other side's ask)
    data.append((b, close, held))


def base_pnl(b):
    won = b["result"] == b["side"]
    ask, n = float(b["ask"]), float(b["contracts"])
    return ((1.0 if won else 0.0) - ask - fee_per_contract(ask)) * n


def hedged(b, close, held, trig, share):
    """(pnl, hedged?) for one bet."""
    for i, (t, bid, _oa) in enumerate(held):
        if t > close - MIN_LEFT:
            break
        if bid <= trig:
            fill = next((h[2] for h in held[i:] if h[0] >= t + LAT), None)
            if fill is None or not 0.02 <= fill <= 0.98:
                return base_pnl(b), False
            n_h = share * float(b["stake_usd"]) / fill
            won = b["result"] == b["side"]
            return base_pnl(b) + ((0.0 if won else 1.0) - fill - fee_per_contract(fill)) * n_h, True
    return base_pnl(b), False


held_total = sum(base_pnl(b) for b, _c, _h in data)
print(f"{len(data)} live commodity bets over {DAYS:g} days, holding every one unhedged: {held_total:+.2f} dollars; worst single loss {min(base_pnl(b) for b, _c, _h in data):+.2f}\n")
print(f"{'trigger':>8} {'share':>6} | {'hedged':>6} {'held won':>8} {'held lost':>9} | {'P&L':>8} {'vs holding':>10} | {'worst bet':>9}")
for trig in TRIGS:
    for share in SHARES:
        res = [hedged(b, c, h, trig, share) for b, c, h in data]
        n_h = sum(1 for _p, x in res if x)
        ww = sum(1 for (b, _c, _h), (_p, x) in zip(data, res) if x and b["result"] == b["side"])
        tot = sum(p for p, _x in res)
        print(f"{trig:>8.2f} {share:>6.2f} | {n_h:>6} {ww:>8} {n_h - ww:>9} | {tot:>+8.2f} {tot - held_total:>+10.2f} | {min(p for p, _x in res):>+9.2f}")
print("DONE")
