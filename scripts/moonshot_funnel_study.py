"""Where does the crypto_moonshot trigger lose its signals, and does any stage have an edge? Replayed from the tape.

The watcher (watcher._on_hunt, the "flip" trigger of a burst_hunt lane) buys the cheap side of a coin's 15-minute market in its last HUNT_WINDOW seconds (not the last HUNT_MIN_LEFT) when, for
a side whose ask is inside the lane's price band (PRICE_LO..PRICE_HI):
    1 BAR      takers bought at least $bar of that side in the last 3 seconds ($400, BTC $800)
    2 SPIKE    ... and that is at least SPIKE times that side's own normal 3 seconds (3 x the median second of the 2 minutes before)
    3 CRASH    ... and its ask has not fallen CRASH or more within the last CRASH_SECONDS (a falling price is the crash, not a turn)
    4 PARKED   ... and it has sat at or under PARK_HIGH for PARK_SECONDS, within PARK_SPAN of the ask now
Each market is walked second by second through its last window; for each stage this reports the markets in which some second passed that stage and all the stages before it, and what the cheap
side did there: its win rate against the average ask, and the P&L of buying one ticket at that ask (taker fee included) at the FIRST second that passed. Dollars are contracts x price from
flow_1s (a YES taker pays the average YES price, a NO taker pays 1 minus it). Stage 0 is simply "a side in the band at some second".

    scripts/cloud.sh run scripts/moonshot_funnel_study.py [DAYS=3] [FAMILY=coins|energy|metals|all]
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
FAMILY = os.environ.get("FAMILY", "coins")
PRICE_LO, PRICE_HI = 0.02, 0.20
HUNT_WINDOW, HUNT_MIN_LEFT = 120, 10
BAR, BAR_BTC, SPIKE = 400.0, 800.0, 5.0
CRASH, CRASH_SECONDS = 0.10, 12
PARK_HIGH, PARK_SECONDS, PARK_SPAN = 0.10, 20, 0.06
COINS = {"BTC", "ETH", "SOL", "XRP", "DOGE"}
ENERGY = {"WTI", "BRENT", "NATGAS"}
METALS = {"GOLD", "SILVER", "PLATINUM", "PALLADIUM", "COPPER"}


def name_of(series):
    m = re.match(r"^KX([A-Z]+)15M$", series)
    return m.group(1) if m else ""


def in_family(series):
    n = name_of(series)
    return n in (COINS | ENERGY | METALS) if FAMILY == "all" else n in {"coins": COINS, "energy": ENERGY, "metals": METALS}[FAMILY]


since = time.time() - DAYS * 86400
wrows = [w for w in main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts >= ? ORDER BY close_ts", (since,)).fetchall() if in_family(w["series"])]
STAGES = ["0 a side in the band", "1 + dollar bar", "2 + 5x its own normal", "3 + not crashing", "4 + parked (the trigger)"]
reach = [[] for _ in STAGES]  # per stage: (ask, won)
events = []  # the first second of each market that passed the dollar bar: (time, series, side, ask, dollars in 3 s, won)
markets = 0
for w in wrows:
    close = float(w["close_ts"])
    quotes = [(float(t), float(b), float(a)) for t, b, a in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 260, close)).fetchall()]
    flow = {}
    for t, yv, nv, vw in tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (w["ticker"], int(close - 260), int(close))).fetchall():
        d = flow.setdefault(int(t), [0.0, 0.0])
        vw = 0.5 if vw is None else float(vw)
        d[0] += float(yv or 0) * vw  # dollars YES takers paid
        d[1] += float(nv or 0) * (1.0 - vw)  # dollars NO takers paid
    if len(quotes) < 10:
        continue
    markets += 1
    bar = BAR_BTC if name_of(w["series"]) == "BTC" else BAR
    first = [None] * len(STAGES)  # (ask, side) at the first second passing each stage
    for t in range(int(close - HUNT_WINDOW), int(close - HUNT_MIN_LEFT)):
        q = [x for x in quotes if x[0] <= t and t - x[0] <= 6]
        if not q:
            continue
        yb, ya = q[-1][1], q[-1][2]
        for side, ask in (("yes", ya), ("no", 1.0 - yb)):
            if not PRICE_LO <= ask <= PRICE_HI:
                continue
            idx = 0 if side == "yes" else 1
            stage = 0
            d3 = sum(flow.get(t - k, [0.0, 0.0])[idx] for k in range(3))
            per_sec = [flow.get(t - 3 - k, [0.0, 0.0])[idx] for k in range(120)]
            normal3 = 3.0 * statistics.median(per_sec)
            asks_hist = [(x[0], x[2] if side == "yes" else 1.0 - x[1]) for x in quotes if t - 40 <= x[0] <= t]
            crashed = any(ask <= a - CRASH for ts_, a in asks_hist if t - CRASH_SECONDS <= ts_ <= t)
            recent = [a for ts_, a in asks_hist if t - PARK_SECONDS <= ts_ <= t]
            parked = len(recent) >= 3 and all(a <= max(PRICE_HI, PARK_HIGH) for a in recent) and (max(recent) - ask) <= PARK_SPAN
            ok = [True, d3 >= bar, d3 >= bar and d3 >= SPIKE * max(normal3, 1.0)]
            ok.append(ok[2] and not crashed)
            ok.append(ok[3] and parked)
            for st in range(len(STAGES)):
                if ok[st] and first[st] is None:
                    first[st] = (ask, side)
                    if st == 1:
                        events.append((t, w["series"], side, ask, d3, w["result"] == side))
    for st, got in enumerate(first):
        if got:
            reach[st].append((got[0], w["result"] == got[1]))

print(f"{markets} {FAMILY} markets over {DAYS:g} days; band {PRICE_LO:.2f}-{PRICE_HI:.2f}, last {HUNT_WINDOW}s (not the last {HUNT_MIN_LEFT}s); bar ${BAR:.0f} (BTC ${BAR_BTC:.0f}), {SPIKE:g}x spike")
print(f"{'stage (each includes the ones above)':<28} {'markets':>7} {'wins':>5} {'win%':>6} {'avg ask':>8} {'P&L/ticket':>11} {'ROI':>7}")
for st, name in enumerate(STAGES):
    rows = reach[st]
    if not rows:
        print(f"{name:<28} {0:>7}")
        continue
    n = len(rows)
    wins = sum(1 for _a, w in rows if w)
    ask = sum(a for a, _w in rows) / n * 100
    pnl = sum((100.0 if w else 0.0) - a * 100 - fee_per_contract(a) * 100 for a, w in rows) / n
    print(f"{name:<28} {n:>7} {wins:>5} {wins / n:>6.1%} {ask:>7.1f}c {pnl:>+10.2f}c {pnl / ask:>+7.0%}")

if os.environ.get("LIST") == "1":
    print("\nthe markets that passed the dollar bar (first second): time UTC, series, cheap side, ask, dollars bought in 3 s, did the side win")
    for t_, ser, side, ask, d3, won in sorted(events):
        print(f"  {time.strftime('%m-%d %H:%M:%S', time.gmtime(t_))}  {name_of(ser):<9} {side:<3} ask {ask:.2f}  ${d3:>7,.0f}  {'WON' if won else 'lost'}")
