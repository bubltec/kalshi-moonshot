"""A burst bar in fixed dollars against a bar that scales with the market's own volume. Replay of the burst exit on every live commodity bet of the last DAYS days. Read-only.

Each second of a bet's last 120 s (not the last MIN_LEFT) is checked. The burst exit SELLS at the bid LAT seconds later (less the taker fee on the sale) when the held side's bid is at least FLOOR and the dollars takers paid
AGAINST the holder over the last 3 seconds (flow_1s: contracts x price paid) pass the rule's bar, with at least SHARE of that window's dollars against the holder:
    fixed $N          a flat dollar bar, the same in every market
    X% of volume      the 3 seconds against the holder are at least X% of ALL dollars traded in this market since it opened (both sides): a bar that grows with a busy market and shrinks for a quiet one
    Nx normal         the 3 seconds are at least N times this market's own normal 3 s (the median second of the 120 s before, times 3), and at least $50
A bet never sold settles at 1 or 0. "vs holding" is the rule's total P&L minus holding every bet to settlement. Rows are by commodity too, so one market's behavior cannot hide in the pool.

    scripts/cloud.sh run scripts/burst_exit_replay2.py [DAYS=3] [LAT=1]       (long: start it in the background on the host)
"""
import os
import re
import statistics
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
RULES = [("fixed $500", "fixed", 500.0), ("fixed $250", "fixed", 250.0), ("fixed $150", "fixed", 150.0),
         ("2% of volume", "share", 0.02), ("4% of volume", "share", 0.04), ("8% of volume", "share", 0.08), ("15% of volume", "share", 0.15),
         ("3x normal", "normal", 3.0), ("6x normal", "normal", 6.0)]
FLOORS = (0.0, 0.85, 0.89)


def name_of(ticker):
    m = re.match(r"^KX([A-Z]+)15M", ticker)
    return m.group(1) if m else ticker


bets = main.db.execute(
    "SELECT d.ticker, d.side, d.ask, d.contracts, w.close_ts, w.result FROM decisions d JOIN windows w ON w.ticker = d.ticker "
    "WHERE d.lane='commodity' AND d.action='bet' AND d.mode='trade' AND d.ts > ? AND w.result IN ('yes','no') AND d.contracts > 0 ORDER BY d.ts", (time.time() - DAYS * 86400,)).fetchall()
data = []
for b in bets:
    close = float(b["close_ts"])
    quotes = [(float(t), float(bid), float(ask)) for t, bid, ask in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (b["ticker"], close - 140, close)).fetchall()]
    flow = {}
    for t, yv, nv, vw in tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (b["ticker"], int(close - 910), int(close))).fetchall():
        d = flow.setdefault(int(t), [0.0, 0.0])
        vw = 0.5 if vw is None else float(vw)
        d[0] += float(yv or 0) * vw
        d[1] += float(nv or 0) * (1.0 - vw)
    if len(quotes) < 5 or not flow:
        continue
    idx = 1 if b["side"] == "yes" else 0  # NO takers against a YES holder, YES takers against a NO holder
    held_bid = lambda t: next(((q[1] if b["side"] == "yes" else 1.0 - q[2]) for q in reversed(quotes) if q[0] <= t), None)  # noqa: E731
    secs_sorted = sorted(flow)
    cum, running = {}, 0.0
    for t in secs_sorted:
        running += sum(flow[t])
        cum[t] = running
    sec = []
    for t in range(int(close - WINDOW_LEFT), int(close - MIN_LEFT)):
        bid = held_bid(t)
        fill = held_bid(t + LAT)
        if bid is None or fill is None:
            continue
        against = sum(flow.get(t - k, [0.0, 0.0])[idx] for k in range(3))
        withh = sum(flow.get(t - k, [0.0, 0.0])[1 - idx] for k in range(3))
        if against <= 0:
            continue
        total = cum.get(max((x for x in secs_sorted if x <= t), default=0), 0.0)
        normal3 = 3.0 * statistics.median([sum(flow.get(t - 3 - k, [0.0, 0.0])) for k in range(120)])
        sec.append((bid, fill, against, against / (against + withh + 1e-9), total, normal3))
    data.append((b, sec))

held_total = 0.0
for b, _sec in data:
    won = b["result"] == b["side"]
    held_total += ((1.0 if won else 0.0) - float(b["ask"]) - fee_per_contract(float(b["ask"]))) * float(b["contracts"])
names = sorted({name_of(b["ticker"]) for b, _ in data})
print(f"{len(data)} live commodity bets over {DAYS:g} days ({', '.join(names)}); holding every one to settlement: {held_total:+.2f} dollars\n")


def fires(kind, val, rec):
    bid, fill, against, share, total, normal3 = rec
    if share < SHARE:
        return False
    if kind == "fixed":
        return against >= val
    if kind == "share":
        return total > 0 and against >= val * total and against >= 50.0
    return against >= val * max(normal3, 1.0) and against >= 50.0


def run(rule, floor, subset):
    label, kind, val = rule
    total = held = 0.0
    sold = ww = 0
    for b, sec in subset:
        ask, n = float(b["ask"]), float(b["contracts"])
        won = b["result"] == b["side"]
        base = ((1.0 if won else 0.0) - ask - fee_per_contract(ask)) * n
        pnl = base
        for rec in sec:
            if rec[0] >= floor and fires(kind, val, rec):
                pnl = (rec[1] - ask - fee_per_contract(ask) - fee_per_contract(rec[1])) * n
                sold += 1
                ww += 1 if won else 0
                break
        total += pnl
        held += base
    return sold, ww, total - held


print(f"{'rule':<16} " + " | ".join(f"floor {f:.2f}: sold/won  vs holding" for f in FLOORS))
for rule in RULES:
    cells = []
    for f in FLOORS:
        sold, ww, vs = run(rule, f, data)
        cells.append(f"{sold:>11}/{ww:<4} {vs:>+10.2f}")
    print(f"{rule[0]:<16} " + " | ".join(cells))
print("\nby commodity, floor 0.89: sold/would have won, vs holding")
print(f"{'rule':<16} " + " ".join(f"{n:>11}" for n in names))
for rule in RULES:
    cells = []
    for n in names:
        sold, ww, vs = run(rule, 0.89, [x for x in data if name_of(x[0]["ticker"]) == n])
        cells.append(f"{sold}/{ww} {vs:+.1f}")
    print(f"{rule[0]:<16} " + " ".join(f"{c:>11}" for c in cells))
print("\nbets per commodity:", {n: sum(1 for x in data if name_of(x[0]["ticker"]) == n) for n in names})
print("DONE")
