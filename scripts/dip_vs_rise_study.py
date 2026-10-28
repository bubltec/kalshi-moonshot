"""DOES THE 15-MINUTE PRICE SNAP BACK OR RUN: if a side's price tends to snap back after it falls, the scalp should buy the DIP (a fall in the last 10-30 s) and not wait for a rise. Two measures on every crypto 15-minute window on the tape:
1. the correlation between a side's price change over the last 10 s and its change over the next 10 s and 30 s (negative = it snaps back, positive = it runs), for sides priced 14c-42c in minutes 0.5-9;
2. every such entry (band 14c-42c, first per side per window; and a version that takes the first moment of each kind of recent move), bought at the ask one second later, the lane's exits as in early_rise_study.py, grouped by how the ask moved over the last 10 s and 30 s: how often the bid
   got 6c over the price paid (the scalp arms), mean net per contract after fees, per dollar staked and the window-clustered standard error. Read-only.
    docker compose exec -T bot python - < scripts/dip_vs_rise_study.py   (SINCE_DAYS=7)
"""
import bisect
import math
import os
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
days = float(os.environ.get("SINCE_DAYS", "7"))
UP, DIP, HARD, HOLD, CASH_MIN, CASH_DROP = 0.06, 0.04, 0.09, 3, 12 * 60, 0.30
CAP = 0.50
def grid(ticker, t0, t1):
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (ticker, t0 - 20, t1)).fetchall()
    ts = [float(r[0]) for r in q]
    out = {}
    for sec in range(int(t0), int(t1) + 1):
        i = bisect.bisect_right(ts, sec + 0.999) - 1
        if i >= 0 and sec + 0.999 - ts[i] <= 15:
            out[sec] = (float(q[i][1]), float(q[i][2]))
    return out


def side_px(g, sec, side):
    x = g.get(sec)
    if x is None:
        return None
    return (x[0], x[1]) if side == "yes" else (1.0 - x[1], 1.0 - x[0])  # (bid, ask) in this side's terms


def exit_value(g, side, e, paid, close, won):
    """net proceeds per contract from entry second e (the buy filled at `paid`)."""
    high, since = 0.0, None
    t0 = close - 900
    for sec in range(e + 1, int(close) - 4):
        px = side_px(g, sec, side)
        if px is None:
            continue
        b = px[0]
        left, el = close - sec, sec - t0
        fire = False
        if (el >= CASH_MIN and b > paid) or (b <= paid - CASH_DROP and el >= CASH_MIN) or (left <= 5 and b < paid):
            fire = True
        if not fire:
            if b >= paid + UP - 1e-9:
                high = max(high, b)
            if high >= paid + UP - 1e-9:
                if high - b >= HARD - 1e-9:
                    fire = True
                elif b <= high - DIP + 1e-9:
                    since = sec if since is None else since
                    fire = sec - since >= HOLD
                else:
                    since = None
        if fire:
            after = side_px(g, sec + 1, side) or px
            return max(after[0], 0.0) - (fee(after[0]) if after[0] > 0 else 0.0)
    return 1.0 if won else 0.0


pairs = []  # (past 10 s change, next 10 s change, next 30 s change) of the ask, this side, in band
rows = []
wins = main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE series IN ('KXBTC15M','KXETH15M','KXSOL15M','KXXRP15M','KXDOGE15M') AND result IN ('yes','no') AND close_ts > extract(epoch from now()) - ?*86400", (days,)).fetchall()
CUTS = [("fell 5c+", -9, -0.05), ("fell 2-5c", -0.05, -0.02), ("flat (within 2c)", -0.02, 0.02), ("rose 2-5c", 0.02, 0.05), ("rose 5c+", 0.05, 9)]
n = 0
for w in wins:
    close = float(w["close_ts"])
    t0 = close - 900
    g = grid(w["ticker"], t0, close)
    if len(g) < 300:
        continue
    n += 1
    for side in ("yes", "no"):
        won = w["result"] == side
        seen = set()
        for sec in range(int(t0 + 30), int(t0 + 540)):
            cur, p10, p30 = side_px(g, sec, side), side_px(g, sec - 10, side), side_px(g, sec - 30, side)
            if cur is None or p10 is None or p30 is None or not (0.14 <= cur[1] <= 0.42):
                continue
            d10, d30 = cur[1] - p10[1], cur[1] - p30[1]
            f10, f30 = side_px(g, sec + 10, side), side_px(g, sec + 30, side)
            if sec % 5 == 0 and f10 is not None and f30 is not None:
                pairs.append((d10, f10[1] - cur[1], f30[1] - cur[1]))
            for key, d in (("d10", d10), ("d30", d30)):
                for cname, lo, hi in CUTS:
                    if lo <= d < hi and (key, cname) not in seen:
                        seen.add((key, cname))
                        fill = side_px(g, sec + 1, side)
                        if fill is None or not (0.05 <= fill[1] <= CAP):
                            continue
                        paid = fill[1]
                        cost = paid + fee(paid)
                        got = exit_value(g, side, sec + 1, paid, close, won)
                        bids = [px[0] for px in (side_px(g, k, side) for k in range(sec + 1, int(close) - 4)) if px is not None]
                        rows.append((key, cname, close, (got - cost), got - cost - 0.0, cost, (max(bids) - paid) if bids else 0.0))
print(f"{n} windows, {days:.0f} days\n")


def corr(a, b):
    ma, mb = statistics.mean(a), statistics.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return num / den if den else float("nan")


print(f"1. does it snap back: {len(pairs)} (side, moment) pairs, price band 14c-42c")
past = [p[0] for p in pairs]
print(f"   correlation of the last 10 s change with the next 10 s change: {corr(past, [p[1] for p in pairs]):+.3f}; with the next 30 s change: {corr(past, [p[2] for p in pairs]):+.3f}")
for cname, lo, hi in CUTS:
    g_ = [p for p in pairs if lo <= p[0] < hi]
    if len(g_) >= 30:
        print(f"   after it {cname:18} n={len(g_):>6}  next 10 s {100 * statistics.mean(p[1] for p in g_):+5.2f}c  next 30 s {100 * statistics.mean(p[2] for p in g_):+5.2f}c")
print("\n2. buying after each kind of recent move (first moment of each kind per side per window)")
for key in ("d10", "d30"):
    print(f"   by the ask's change over the last {key[1:]} s")
    print(f"   {'group':22} {'n':>5} {'bid +6c':>8} {'avg peak':>9} {'net/contract':>13} {'per $ staked':>13} {'+-se':>6}")
    for cname, *_ in CUTS:
        r = [x for x in rows if x[0] == key and x[1] == cname]
        if len(r) < 30:
            print(f"   {cname:22} {len(r):>5}")
            continue
        per = [x[3] / x[5] for x in r]
        by = defaultdict(float)
        for x, v in zip(r, per, strict=True):
            by[x[2]] += v
        se = math.sqrt(len(by)) * statistics.pstdev(list(by.values())) / len(r)
        print(f"   {cname:22} {len(r):>5} {100 * sum(x[6] >= UP - 1e-9 for x in r) / len(r):>7.0f}% {100 * statistics.mean(x[6] for x in r):>+8.1f}c {100 * statistics.mean(x[3] for x in r):>+12.1f}c {100 * statistics.mean(per):>+12.1f}% {100 * se:>5.1f}%")
