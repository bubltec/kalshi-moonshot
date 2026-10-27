"""SHOULD THE FAVORITE LANES SELL ON THE MOONSHOT'S BURST: holding a favorite (90c+) in the last 120 s, the burst exit sells it when takers go against it. This compares, per family (BTC, other crypto, currencies, metals and energy), two ways to spot that burst, by what selling the favorite at its bid
earned against holding it to the result, per contract (sale net of the taker fee, minus what holding pays: 1 if it won, 0 if not):
  NEW  = at least $X bought against the favorite in the last 3 s (the moonshot's trigger; X on the grid below)
  OLD  = the lane rule: the against-side contracts in 3 s are 5x or more their own median second (300+ contracts, a normal second of 10+), the favorite's bid 80-97c.
Per market the first signal in the last 120 s (10+ s left). Positive = selling beat holding. Errors clustered by window.
Read-only.
    docker compose exec -T bot python - < scripts/burst_exit_study.py
"""
import bisect
import math
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
BARS = (10, 25, 50, 100, 200, 400, 800)


def family(series):
    if series.startswith("KXBTC"):
        return "BTC"
    if series[2:].startswith(("ETH", "SOL", "XRP", "DOGE")):
        return "other crypto"
    return "currencies" if any(x in series for x in ("EUR", "GBP", "JPY", "AUD", "CAD")) else "metals/energy"


events = defaultdict(list)  # (family, rule) -> [(window, advantage)]
n_win = defaultdict(int)
for w in main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE series LIKE '%15M' AND result IN ('yes','no') AND close_ts > ?", (float(tape.execute("SELECT MIN(ts) FROM flow_1s").fetchone()[0]) + 400,)).fetchall():
    T = float(w["close_ts"])
    fam = family(w["series"])
    flow = {int(x[0]): x for x in tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (w["ticker"], T - 260, T)).fetchall()}
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], T - 260, T)).fetchall()
    if len(flow) < 15 or len(q) < 30:
        continue
    n_win[fam] += 1
    qts = [x[0] for x in q]

    def vals(sec, taker, flow=flow):
        x = flow.get(sec)
        if not x or x[3] is None:
            return 0.0, 0.0
        return (x[1], x[1] * x[3]) if taker == "yes" else (x[2], x[2] * (1.0 - x[3]))
    fired = set()
    for sec in range(int(T) - 120, int(T) - 9):
        i = bisect.bisect_right(qts, sec) - 1
        if i < 0 or sec - qts[i] > 20:
            continue
        yb, ya = q[i][1], q[i][2]
        mid = (yb + ya) / 2
        fav = "yes" if mid >= 0.5 else "no"
        fav_bid = yb if fav == "yes" else 1.0 - ya
        if max(mid, 1 - mid) < 0.90 and not (0.80 <= fav_bid <= 0.97):
            continue
        against = "no" if fav == "yes" else "yes"
        c3 = sum(vals(sec - k, against)[0] for k in range(3))
        d3 = sum(vals(sec - k, against)[1] for k in range(3))
        won = w["result"] == fav
        adv = (fav_bid - fee(fav_bid)) - (1.0 if won else 0.0)
        if max(mid, 1 - mid) >= 0.90:
            for bar in BARS:
                if d3 >= bar and ("new", bar) not in fired:
                    fired.add(("new", bar))
                    events[(fam, f"NEW ${bar}")].append((T, adv))
        if "old" not in fired and 0.80 <= fav_bid <= 0.97:
            per = sorted(vals(sec - 3 - k, against)[0] for k in range(120))
            base = (per[59] + per[60]) / 2
            if c3 / (3 * base + 1.0) >= 5.0 and c3 >= 300 and base >= 10:
                fired.add("old")
                events[(fam, "OLD (live rule)")].append((T, adv))
print({k: v for k, v in n_win.items()}, "windows")
for fam in ("BTC", "other crypto", "currencies", "metals/energy"):
    print(f"\n{fam}: selling the favorite at the signal against holding it, cents per contract (+ = selling won)")
    for rule in ["OLD (live rule)"] + [f"NEW ${b}" for b in BARS]:
        ev = events.get((fam, rule), [])
        if len(ev) < 6:
            print(f"  {rule:16} {len(ev):3} signals (too few)")
            continue
        by = defaultdict(float)
        for t, a in ev:
            by[t] += a
        m = statistics.mean(a for _, a in ev)
        se = math.sqrt(len(by)) * statistics.pstdev(list(by.values())) / len(ev) if len(by) > 1 else float("nan")
        print(f"  {rule:16} {len(ev):3} signals ({len(ev) / max(n_win[fam], 1):4.0%} of windows): {100 * m:+6.1f}c +-{100 * se:.1f}c")
