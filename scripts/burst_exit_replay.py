"""Replay the burst exit on every live commodity bet of the last DAYS days, with different floors, windows and bars. Read-only.

A bet is held from its entry. Each second of its last 120 s (and not the last MIN_LEFT) is checked: the burst exit SELLS at the bid LAT seconds later when
    the held side's bid is at least FLOOR (0 = no floor, how the lane runs now),
    the dollars takers paid AGAINST the holder over the last WINDOW seconds are at least BAR (flow_1s: contracts x price paid), and
    at least SHARE of all the dollars in that window were against the holder.
A sale gets the bid, less the taker fee on the sale. A bet never sold settles at 1 or 0. The "sold" and "would have won" columns count bets sold; "vs holding" is the total P&L of the rule minus the total of
holding every bet to settlement, so a positive number is money the rule made against doing nothing. Rows are by FLOOR, WINDOW, BAR; the first row is how the lane runs now (0, 3, 400).

    scripts/cloud.sh run scripts/burst_exit_replay.py [DAYS=3] [LAT=1]
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

DAYS = float(os.environ.get("DAYS", "3"))
LAT = int(os.environ.get("LAT", "1"))
MIN_LEFT, WINDOW_LEFT, SHARE = 10, 120, 0.75
GRID = list(itertools.product((0.0, 0.70, 0.80, 0.85, 0.90), (1, 2, 3), (150.0, 250.0, 400.0)))

bets = main.db.execute(
    "SELECT d.ticker, d.side, d.ask, d.contracts, w.close_ts, w.result FROM decisions d JOIN windows w ON w.ticker = d.ticker "
    "WHERE d.lane='commodity' AND d.action='bet' AND d.mode='trade' AND d.ts > ? AND w.result IN ('yes','no') AND d.contracts > 0 ORDER BY d.ts", (time.time() - DAYS * 86400,)).fetchall()
data = []
for b in bets:
    close = float(b["close_ts"])
    quotes = [(float(t), float(bid), float(ask)) for t, bid, ask in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (b["ticker"], close - 140, close)).fetchall()]
    flow = {}
    for t, yv, nv, vw in tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (b["ticker"], int(close - 140), int(close))).fetchall():
        d = flow.setdefault(int(t), [0.0, 0.0])
        vw = 0.5 if vw is None else float(vw)
        d[0] += float(yv or 0) * vw
        d[1] += float(nv or 0) * (1.0 - vw)
    if len(quotes) < 5:
        continue
    held_bid = lambda t: next(((q[1] if b["side"] == "yes" else 1.0 - q[2]) for q in reversed(quotes) if q[0] <= t), None)  # noqa: E731
    sec = []
    for t in range(int(close - WINDOW_LEFT), int(close - MIN_LEFT)):
        bid = held_bid(t)
        if bid is None:
            continue
        against_idx = 1 if b["side"] == "yes" else 0
        sec.append((t, bid, [flow.get(t - k, [0.0, 0.0]) for k in range(3)], against_idx, held_bid(t + LAT)))
    data.append((b, sec))

held_total = 0.0
for b, _sec in data:
    won = b["result"] == b["side"]
    held_total += ((1.0 if won else 0.0) - float(b["ask"]) - fee_per_contract(float(b["ask"]))) * float(b["contracts"])
print(f"{len(data)} live commodity bets over {DAYS:g} days; holding every one to settlement: {held_total:+.2f} dollars\n")
print(f"{'floor':>6} {'window':>6} {'bar':>5} | {'sold':>4} {'would win':>9} {'P&L':>8} {'vs holding':>10} | {'sold at':>7}")
out = []
for floor, win, bar in GRID:
    total, sold, wouldwin, px = 0.0, 0, 0, 0.0
    for b, sec in data:
        ask, n = float(b["ask"]), float(b["contracts"])
        won = b["result"] == b["side"]
        pnl = ((1.0 if won else 0.0) - ask - fee_per_contract(ask)) * n
        for t, bid, rows, idx, fill_bid in sec:
            if bid < floor or fill_bid is None:
                continue
            against = sum(r[idx] for r in rows[:win])
            withh = sum(r[1 - idx] for r in rows[:win])
            if against >= bar and against / (against + withh + 1e-9) >= SHARE:
                pnl = (fill_bid - ask - fee_per_contract(ask) - fee_per_contract(fill_bid)) * n
                sold += 1
                wouldwin += 1 if won else 0
                px += fill_bid
                break
        total += pnl
    out.append((floor, win, bar, sold, wouldwin, total, total - held_total, px / sold if sold else 0.0))
for floor, win, bar, sold, ww, total, vs, px in sorted(out, key=lambda r: (r[0] != 0.0 or r[1] != 3 or r[2] != 400.0, -r[6])):
    mark = "  <== lane now" if (floor, win, bar) == (0.0, 3, 400.0) else ""
    print(f"{floor:>6.2f} {win:>5}s {bar:>5.0f} | {sold:>4} {ww:>9} {total:>+8.2f} {vs:>+10.2f} | {px:>6.2f}{mark}")
