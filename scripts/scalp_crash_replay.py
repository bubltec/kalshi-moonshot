"""Buy a crash: a side whose ask falls from HIGH or more to LEVEL or under inside W seconds (a 3-5 s collapse, not a drift) gets a buy limit at LEVEL for TIMEOUT seconds; a fill puts a resting sell GAIN over the fill price until the close. Replay on every crypto
15-minute window of the last DAYS days. Read-only.

Trigger: the first second a side's ask is at or under LEVEL while its ask W seconds earlier (any second inside the last W) was at least HIGH. One per market (the first side to crash). The buy limit goes in one second later (LAT) and fills the first second inside TIMEOUT s that the side's
ask is at or under LEVEL, at that ask, taker fee paid (FEE=0: fee-free resting). The sale rests at the fill price plus GAIN and fills there the first second the held bid reaches it (maker, no fee unless SELLFEE=1). The sale rests at the fill price plus GAIN. A fill never sold is held to the result (1 or 0). An unfilled buy is no bet.
Events are listed by minutes left at the trigger, because a crash with seconds to go is a settlement, not a dip.

    scripts/cloud.sh run scripts/scalp_crash_replay.py [DAYS=7] [HIGH=0.50] [TIMEOUT=10]
    docker compose ... exec -T -e DAYS=3 bot python - < scripts/scalp_crash_replay.py
"""
import bisect
import math
import os
import re
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

DAYS = float(os.environ.get("DAYS", "7"))
HIGH, TIMEOUT = float(os.environ.get("HIGH", "0.50")), int(os.environ.get("TIMEOUT", "10"))
FEE = os.environ.get("FEE", "1") == "1"
SELLFEE = os.environ.get("SELLFEE", "0") == "1"
LAT, MIN_LEFT = 1, 15
COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")
LEVELS = (0.20, 0.25, 0.30)
WINDOWS = (3, 5)
GAINS = (0.05, 0.10, 0.15, 0.20)  # the sale rests at the price paid plus this

wins = main.db.execute("SELECT ticker, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (time.time() - DAYS * 86400,)).fetchall()
markets = []
for w in wins:
    m = re.match(r"^KX([A-Z]+)15M-", w["ticker"])
    if not m or m.group(1) not in COINS:
        continue
    close = float(w["close_ts"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 900, close)).fetchall()
    if len(q) < 30:
        continue
    ts = [float(r[0]) for r in q]
    per = {}
    for sec in range(int(close - 900), int(close) + 1):
        i = bisect.bisect_right(ts, sec + 0.999) - 1
        if i >= 0 and sec + 0.999 - ts[i] <= 15:
            per[sec] = (float(q[i][1]), float(q[i][2]))
    markets.append((close, w["result"], per))
print(f"{len(markets)} crypto windows over {DAYS:g} days; crash from {HIGH:.2f}+, buy limit timeout {TIMEOUT} s, buy fee {'on' if FEE else 'off'}, sale fee {'on' if SELLFEE else 'off (maker)'}\n")


def held(per, sec, side):
    v = per.get(sec)
    if v is None:
        return None
    return (v[0], v[1]) if side == "yes" else (1.0 - v[1], 1.0 - v[0])  # (held bid, held ask)


def trigger(close, per, level, w):
    for sec in range(int(close - 900) + w, int(close) - MIN_LEFT):
        for side in ("yes", "no"):
            now = held(per, sec, side)
            if now is None or now[1] > level:
                continue
            past = [held(per, sec - k, side) for k in range(1, w + 1)]
            if any(p is not None and p[1] >= HIGH for p in past):
                return sec, side
    return None


def buy(close, per, sec0, side, level):
    for sec in range(sec0 + LAT, min(sec0 + LAT + TIMEOUT, int(close) - MIN_LEFT)):
        h = held(per, sec, side)
        if h is not None and h[1] <= level + 1e-9:
            return sec, h[1]
    return None


def sold_at(close, per, sec1, side, target):
    for sec in range(sec1 + 1, int(close) - 2):
        h = held(per, sec, side)
        if h is not None and h[0] >= target - 1e-9:
            return True
    return False


def avg(xs):
    return sum(xs) / len(xs) * 100 if xs else float("nan")


print(f"{'crash':>12} {'limit':>5} {'gain':>6} | {'events':>6} {'filled':>6} {'sold':>5} | {'per fill':>8} {'+-':>5} | {'sold avg':>8} {'held avg':>8} | {'older':>7} {'newer':>7}")
shown = {}
for w in WINDOWS:
    for level in LEVELS:
        ev = [(close, result, per, t) for close, result, per in markets for t in [trigger(close, per, level, w)] if t]
        fills = []
        for close, result, per, (sec0, side) in ev:
            f = buy(close, per, sec0, side, level)
            if f:
                fills.append((close, result, per, side, f[0], f[1], close - sec0))
        shown[(w, level)] = fills
        for gain in GAINS:
            rows = []
            for close, result, per, side, sec1, ask, left in fills:
                target = min(ask + gain, 0.99)
                sold = sold_at(close, per, sec1, side, target)
                cost = ask + (fee_per_contract(ask) if FEE else 0.0)
                pnl = (target - (fee_per_contract(target) if SELLFEE else 0.0) - cost) if sold else ((1.0 if result == side else 0.0) - cost)
                rows.append((pnl, close, sold))
            if not rows:
                print(f"{HIGH * 100:>3.0f}c+/{w}s {level:>5.2f} {gain:>6.2f} | {len(ev):>6} {0:>6}")
                continue
            n = len(rows)
            p = [r[0] for r in rows]
            mean = sum(p) / n
            se = (sum((x - mean) ** 2 for x in p) / n) ** 0.5 / math.sqrt(n) * 100
            order = sorted(range(n), key=lambda i: rows[i][1])
            a, b = [p[i] for i in order[: n // 2]], [p[i] for i in order[n // 2:]]
            ns = sum(1 for r in rows if r[2])
            print(f"{HIGH * 100:>3.0f}c+/{w}s {level:>5.2f} {gain:>6.2f} | {len(ev):>6} {n:>6} {ns:>5} | {mean * 100:>+7.2f}c {se:>5.2f} | {avg([r[0] for r in rows if r[2]]):>+7.2f}c {avg([r[0] for r in rows if not r[2]]):>+7.2f}c | {avg(a):>+6.2f}c {avg(b):>+6.2f}c")
fills = shown[(5, 0.25)]
print("\nfilled crashes (5 s, limit 0.25) by minutes left at the trigger: count, share that went on to win the market")
for lo, hi in ((0, 1), (1, 3), (3, 6), (6, 15)):
    rs = [f for f in fills if lo * 60 <= f[6] < hi * 60]
    if rs:
        print(f"  {lo}-{hi} min left: {len(rs):>3} filled, held side won {100 * sum(1 for f in rs if f[1] == f[3]) / len(rs):.0f}% (paid avg {sum(f[5] for f in rs) / len(rs):.2f})")
print("DONE")
