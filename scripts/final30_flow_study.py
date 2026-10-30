"""Does taker flow in the last 30 seconds of a 15-minute market tell us the settlement better than the price does? Replayed from the tape.

At each checkpoint T (seconds before the close) the PRESSURE side is the side takers bought more dollars of over the last WIN seconds (dollars = contracts x price paid, from flow_1s). We "buy" that side
LAT seconds later at the ask then, held to settlement, taker fee included. The result is split by the price paid (cheap sides, coin flips, favorites), by how unusual the flow is against the market's own
normal (ratio of the last WIN seconds to the median second of the 120 seconds before, scaled), and by the older half of the days against the newer half, so an edge has to show in both to count.
The same table is printed for FADE (the opposite side), which is the other reading of the same flow.

    scripts/cloud.sh run scripts/final30_flow_study.py [DAYS=7] [FAMILY=coins|energy] [WIN=5] [LAT=2] [MIN_D=150]
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

DAYS = float(os.environ.get("DAYS", "7"))
FAMILY = os.environ.get("FAMILY", "coins")
WIN = int(os.environ.get("WIN", "5"))
LAT = int(os.environ.get("LAT", "2"))
MIN_D = float(os.environ.get("MIN_D", "150"))
CHECKPOINTS = (30, 20, 12)
BUCKETS = ((0.02, 0.20, "2-20c"), (0.20, 0.50, "20-50c"), (0.50, 0.80, "50-80c"), (0.80, 0.97, "80-97c"))
RATIOS = ((0.0, "any"), (3.0, ">=3x"), (8.0, ">=8x"))
FAMILIES = {"coins": {"BTC", "ETH", "SOL", "XRP", "DOGE"}, "energy": {"WTI", "BRENT", "NATGAS"}}
FAMILIES["all"] = FAMILIES["coins"] | FAMILIES["energy"]


def name_of(series):
    m = re.match(r"^KX([A-Z]+)15M$", series)
    return m.group(1) if m else ""


since = time.time() - DAYS * 86400
wrows = [w for w in main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts >= ? ORDER BY close_ts", (since,)).fetchall() if name_of(w["series"]) in FAMILIES[FAMILY]]
mid_ts = wrows[len(wrows) // 2]["close_ts"] if wrows else 0
by_name = []  # (name, T, mode, half, ask, won): every pressure-side ticket priced 2-97c
rows = []  # (T, mode, bucket, ratio_floor, half, ask, won)
markets = 0
for w in wrows:
    close = float(w["close_ts"])
    quotes = [(float(t), float(b), float(a)) for t, b, a in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 60, close)).fetchall()]
    flow = {}
    for t, yv, nv, vw in tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?", (w["ticker"], int(close - 200), int(close))).fetchall():
        d = flow.setdefault(int(t), [0.0, 0.0])
        vw = 0.5 if vw is None else float(vw)
        d[0] += float(yv or 0) * vw
        d[1] += float(nv or 0) * (1.0 - vw)
    if len(quotes) < 5 or not flow:
        continue
    markets += 1
    half = close  # turned into old/new at the median of the markets actually used
    for T in CHECKPOINTS:
        t = int(close - T)
        yd = sum(flow.get(t - k, [0.0, 0.0])[0] for k in range(WIN))
        nd = sum(flow.get(t - k, [0.0, 0.0])[1] for k in range(WIN))
        if max(yd, nd) < MIN_D or yd == nd:
            continue
        per_sec = [sum(flow.get(t - WIN - k, [0.0, 0.0])) for k in range(120)]
        normal = max(statistics.median(per_sec) * WIN, 1.0)
        ratio = (yd + nd) / normal
        pressure = "yes" if yd > nd else "no"
        q = [x for x in quotes if x[0] <= t + LAT and t + LAT - x[0] <= 6]
        if not q:
            continue
        for mode, side in (("follow", pressure), ("fade", "no" if pressure == "yes" else "yes")):
            ask = q[-1][2] if side == "yes" else 1.0 - q[-1][1]
            if 0.02 < ask <= 0.97:
                by_name.append((name_of(w["series"]), T, mode, half, ask, w["result"] == side))
            for lo, hi, label in BUCKETS:
                if lo < ask <= hi:
                    for rfloor, _name in RATIOS:
                        if ratio >= rfloor:
                            rows.append((T, mode, label, rfloor, half, ask, w["result"] == side))

used = sorted({r[4] for r in rows} | {r[3] for r in by_name})
mid = used[len(used) // 2] if used else 0
rows = [r[:4] + ("old" if r[4] < mid else "new",) + r[5:] for r in rows]
by_name = [r[:3] + ("old" if r[3] < mid else "new",) + r[4:] for r in by_name]
print(f"{markets} {FAMILY} markets over {DAYS:g} days; pressure = more taker dollars over the last {WIN}s (at least ${MIN_D:.0f}); bought {LAT}s later at the ask, held, fee included")
print(f"{'T':>3} {'mode':<6} {'price':<7} {'ratio':<5} | {'n':>4} {'win%':>6} {'ask':>6} {'edge/ticket':>11} {'ROI':>6} | {'old n':>5} {'old edge':>8} | {'new n':>5} {'new edge':>8}")


def edge(group):
    n = len(group)
    pnl = sum((100.0 if won else 0.0) - a * 100 - fee_per_contract(a) * 100 for *_, a, won in group) / n
    ask = sum(g[-2] for g in group) / n * 100
    return n, sum(1 for g in group if g[-1]) / n, ask, pnl


for T in CHECKPOINTS:
    for mode in ("follow", "fade"):
        for _lo, _hi, label in BUCKETS:
            for rfloor, rname in RATIOS:
                g = [r for r in rows if r[0] == T and r[1] == mode and r[2] == label and r[3] == rfloor]
                if len(g) < 12:
                    continue
                n, win, ask, pnl = edge(g)
                old = [r for r in g if r[4] == "old"]
                new = [r for r in g if r[4] == "new"]
                oe = f"{edge(old)[3]:>+7.1f}c" if len(old) >= 5 else "      -"
                ne = f"{edge(new)[3]:>+7.1f}c" if len(new) >= 5 else "      -"
                flag = "  <==" if pnl > 0 and all(len(x) >= 5 and edge(x)[3] > 0 for x in (old, new)) else ""
                print(f"{T:>3} {mode:<6} {label:<7} {rname:<5} | {n:>4} {win:>6.1%} {ask:>5.1f}c {pnl:>+10.2f}c {pnl / ask:>+6.0%} | {len(old):>5} {oe} | {len(new):>5} {ne}{flag}")


print(f"\nPER MARKET (every pressure-side ticket priced 2-97c, flow of at least ${MIN_D:.0f}); <== = positive in both halves")
print(f"{'market':<8} {'T':>3} {'mode':<6} | {'n':>4} {'win%':>6} {'ask':>6} {'edge/ticket':>11} {'ROI':>6} | {'old n':>5} {'old edge':>8} | {'new n':>5} {'new edge':>8}")
for nm in sorted({r[0] for r in by_name}):
    for T in CHECKPOINTS:
        for mode in ("follow", "fade"):
            g = [(r[4], r[5]) for r in by_name if r[0] == nm and r[1] == T and r[2] == mode]
            if len(g) < 12:
                continue
            n, win, ask, pnl = edge([("x", a, w) for a, w in g])
            old = [("x", r[4], r[5]) for r in by_name if r[0] == nm and r[1] == T and r[2] == mode and r[3] == "old"]
            new = [("x", r[4], r[5]) for r in by_name if r[0] == nm and r[1] == T and r[2] == mode and r[3] == "new"]
            oe = f"{edge(old)[3]:>+7.1f}c" if len(old) >= 5 else "      -"
            ne = f"{edge(new)[3]:>+7.1f}c" if len(new) >= 5 else "      -"
            flag = "  <==" if pnl > 0 and all(len(x) >= 5 and edge(x)[3] > 0 for x in (old, new)) else ""
            print(f"{nm:<8} {T:>3} {mode:<6} | {n:>4} {win:>6.1%} {ask:>5.1f}c {pnl:>+10.2f}c {pnl / ask:>+6.0%} | {len(old):>5} {oe} | {len(new):>5} {ne}{flag}")
