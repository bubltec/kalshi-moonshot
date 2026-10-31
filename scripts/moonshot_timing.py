"""Does the moonshot do worse when it buys late? The lane's own trigger replayed with a minimum time left. Read-only.

The same candidates and gates as moonshot_review.py at the lane's current settings (bar $400, BTC $800, a 5x spike over the market's own normal, the crash and parked checks on, cheap side up to 20c). For each MIN seconds, a market's buy is its first second that passes the
gates with at least MIN seconds still to the close (the lane's own window is the last 120 s down to 10 s); the table gives the events, win rate, mean ask and edge per ticket after the fee, with the older and newer half of the days, so it shows how the result changes if the lane
refuses to buy in the last 20, 30, 45, 60 or 75 seconds. A second table splits the buys the lane makes today by how many seconds were left.

    scripts/cloud.sh run scripts/moonshot_timing.py [DAYS=7] [BURST_MODE=dollars|contracts] [BAR=400] [SPIKE=5] [BTCX=2.0] [GATES=on|off] [LO=0.02] [HI=0.20]
"""
import itertools
import os
import re
import statistics
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.lanes import enabled_lanes
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

DAYS = float(os.environ.get("DAYS", "7"))
MIN_EVENTS = 15
LO = float(os.environ.get("LO", "0.02"))
WINDOW, MIN_LEFT = 120, 10

FAMILIES = {"coins": {"BTC", "ETH", "SOL", "XRP", "DOGE"}, "energy": {"WTI", "BRENT", "NATGAS"}}
CRASH, CRASH_SECONDS, PARK_SECONDS, PARK_SPAN, PARK_HIGH = 0.10, 12, 20, 0.06, 0.10
MODE = os.environ.get("BURST_MODE", "dollars")  # what the burst is counted in: dollars (contracts x price paid, the lane today) or contracts
GATES = os.environ.get("GATES", "on") == "on"  # the crash and parked checks (the lane had them on; hunt_crash 1.0 and a wide-open park turn them off)
BTCX = float(os.environ.get("BTCX", "2.0"))  # BTC's bar as a multiple of the others (800/400 before, 4000/1500 now)
BAR, SPIKE = float(os.environ.get("BAR", "400")), float(os.environ.get("SPIKE", "5"))
LO, HI = float(os.environ.get("LO", "0.02")), float(os.environ.get("HI", "0.20"))  # the cheap side's ask range


def name_of(series):
    m = re.match(r"^KX([A-Z]+)15M$", series)
    return m.group(1) if m else ""


since = time.time() - DAYS * 86400
wrows = main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts >= ? ORDER BY close_ts", (since,)).fetchall()
print(f"moonshot timing, {DAYS:g} days of the tape; burst counted in {MODE}, bar {BAR:g}, spike {SPIKE:g}x, crash and parked checks {"on" if GATES else "off"}\n")
for fam, names in FAMILIES.items():
    cand = []  # (close, result, [(left, side, ask)])
    for w in wrows:
        nm = name_of(w["series"])
        if nm not in names:
            continue
        close = float(w["close_ts"])
        quotes = [(float(t), float(b), float(a)) for t, b, a in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 260, close)).fetchall()]
        flow = {}
        for t, yv, nv, vw in tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (w["ticker"], int(close - 260), int(close))).fetchall():
            d = flow.setdefault(int(t), [0.0, 0.0])
            vw = 0.5 if vw is None else float(vw)
            if MODE == "contracts":
                d[0] += float(yv or 0)
                d[1] += float(nv or 0)
            else:
                d[0] += float(yv or 0) * vw
                d[1] += float(nv or 0) * (1.0 - vw)
        if len(quotes) < 10 or not flow:
            continue
        rows = []
        for t in range(int(close - 120), int(close - 10)):
            q = [x for x in quotes if x[0] <= t and t - x[0] <= 6]
            if not q:
                continue
            yb, ya = q[-1][1], q[-1][2]
            for side, ask in (("yes", ya), ("no", 1.0 - yb)):
                if not LO <= ask <= HI:
                    continue
                idx = 0 if side == "yes" else 1
                d3 = sum(flow.get(t - k, [0.0, 0.0])[idx] for k in range(3))
                if d3 < BAR * (BTCX if nm == "BTC" and MODE == "dollars" else 1.0):
                    continue
                normal3 = 3.0 * statistics.median([flow.get(t - 3 - k, [0.0, 0.0])[idx] for k in range(120)])
                if d3 / max(normal3, 1.0) < SPIKE:
                    continue
                hist = [(x[0], x[2] if side == "yes" else 1.0 - x[1]) for x in quotes if t - 40 <= x[0] <= t]
                crashed = any(ask <= a - CRASH for ts_, a in hist if t - CRASH_SECONDS <= ts_ <= t)
                recent = [a for ts_, a in hist if t - PARK_SECONDS <= ts_ <= t]
                rmax = max(recent) if recent else 1.0
                if GATES and (crashed or len(recent) < 3 or rmax > max(HI, PARK_HIGH) or rmax > ask + PARK_SPAN):
                    continue
                rows.append((close - t, side, ask))
        if rows:
            cand.append((close, w["result"], rows))
    mid = sorted(c[0] for c in cand)[len(cand) // 2] if cand else 0

    def edge(g):
        return sum((100.0 if won else 0.0) - a * 100 - fee_per_contract(a) * 100 for _c, a, won in g) / len(g)

    print(f"== {fam}: {len(cand)} markets with a qualifying burst")
    print(f"  {'buy only with at least':>24} | {'events':>6} {'wins':>5} {'win%':>6} {'ask':>6} {'edge/ticket':>11} {'ROI':>6} | {'old':>8} {'new':>8}")
    for mn in (10, 20, 30, 45, 60, 75, 90):
        picks = []
        for close, result, rows in cand:
            for left, side, ask in rows:
                if left >= mn:
                    picks.append((close, ask, result == side))
                    break
        if not picks:
            continue
        n, wins = len(picks), sum(1 for p in picks if p[2])
        ask = sum(p[1] for p in picks) / n * 100
        old, new = [p for p in picks if p[0] < mid], [p for p in picks if p[0] >= mid]
        oe = f"{edge(old):>+7.1f}c" if len(old) >= 4 else "       -"
        ne = f"{edge(new):>+7.1f}c" if len(new) >= 4 else "       -"
        print(f"  {mn:>22} s | {n:>6} {wins:>5} {wins / n:>6.1%} {ask:>5.1f}c {edge(picks):>+10.2f}c {edge(picks) / ask:>+6.0%} | {oe} {ne}")
    print(f"  the lane's buys today (first pass of the gates), by seconds left:")
    first = []
    for close, result, rows in cand:
        left, side, ask = rows[0]
        first.append((left, ask, result == side, close))
    for lo, hi in ((10, 30), (30, 60), (60, 90), (90, 121)):
        g = [(c, a, w) for left, a, w, c in first if lo <= left < hi]
        if g:
            n, wins = len(g), sum(1 for p in g if p[2])
            print(f"    {lo:>3}-{hi - 1 if hi == 121 else hi:>3} s left | {n:>4} buys, {wins:>3} won ({wins / n:>5.1%}), ask {sum(p[1] for p in g) / n * 100:>5.1f}c, edge {edge(g):>+7.2f}c")
    print()
print("DONE")
