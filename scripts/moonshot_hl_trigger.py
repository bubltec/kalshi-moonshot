"""Hyperliquid volume as the moonshot trigger: buy the cheap side when the futures' own taker flow surges toward it, without waiting for the Kalshi dollar bar. Read-only.

For each crypto 15-minute market the last WINDOW seconds are walked second by second down to MIN_LEFT s. A side is the cheap side when its ask is in [LO, HI]. The Hyperliquid coin's taker flow (tape.fut_1s: buy_usd - sell_usd, the dollars takers lifted
minus hit) over the last 3 s is the signal: an up move favors the YES side, a down move the NO side. The surge is the 3 s SIGNED dollars toward the cheap side against this coin's own normal 3 s, the median of the |signed| 3 s sums over the 300 s before, at least RATIO times it and at least
FLOOR dollars. The first second a cheap side has the surge is the buy, at its ask one second later (LAT) with the taker fee; held to the result. BASELINE is the same markets bought the first second the cheap side is in the range with no surge condition, so the table shows what the surge adds.
Events are listed with how many the lane's Kalshi dollar trigger would also have had (same market, same side).

    scripts/cloud.sh run scripts/moonshot_hl_trigger.py [DAYS=3] [LO=0.02] [HI=0.20]
"""
import bisect
import math
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
LO, HI = float(os.environ.get("LO", "0.02")), float(os.environ.get("HI", "0.20"))
WINDOW, MIN_LEFT, LAT = 120, 10, 1
COINS = {"BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "XRP": "XRP", "DOGE": "DOGE"}
RATIOS = (3.0, 5.0, 10.0, 20.0)
FLOORS = {"BTC": 200000.0, "ETH": 100000.0, "SOL": 50000.0, "XRP": 30000.0, "DOGE": 20000.0}

first_ts = tape.execute("SELECT min(ts) FROM fut_1s").fetchone()[0]
wins = main.db.execute("SELECT ticker, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts > ? ORDER BY close_ts", (max(time.time() - DAYS * 86400, float(first_ts or 0) + 400),)).fetchall()
markets = []
for w in wins:
    m = re.match(r"^KX([A-Z]+)15M-", w["ticker"])
    if not m or m.group(1) not in COINS:
        continue
    coin, close = m.group(1), float(w["close_ts"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid >= 0 AND ask > 0 AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 140, close)).fetchall()
    fl = tape.execute("SELECT ts, buy_usd, sell_usd FROM fut_1s WHERE symbol=? AND ts BETWEEN ? AND ?", (COINS[coin], int(close - WINDOW - 320), int(close))).fetchall()
    if len(q) < 10 or len(fl) < 30:
        continue
    ts = [float(r[0]) for r in q]
    per = {}
    for sec in range(int(close - 140), int(close) + 1):
        i = bisect.bisect_right(ts, sec + 0.999) - 1
        if i >= 0 and sec + 0.999 - ts[i] <= 15:
            per[sec] = (float(q[i][1]), float(q[i][2]))
    signed = {int(t): float(b or 0) - float(sl or 0) for t, b, sl in fl}
    markets.append((w["ticker"], coin, close, w["result"], per, signed))
print(f"{len(markets)} crypto markets with Kalshi quotes and Hyperliquid flow ({DAYS:g} days max)\n")


def asks(per, sec):
    v = per.get(sec)
    return None if v is None else {"yes": v[1], "no": 1.0 - v[0]}  # each side's ask


def sum3(signed, sec):
    return sum(signed.get(sec - k, 0.0) for k in range(3))


def play(close, result, per, sec, side):
    a = asks(per, sec + LAT)
    if a is None or not LO <= a[side] <= HI + 0.03:
        return None
    ask = a[side]
    return (1.0 if result == side else 0.0) - ask - fee_per_contract(ask), ask


def find(market, ratio, floor_on):
    ticker, coin, close, result, per, signed = market
    for sec in range(int(close - WINDOW), int(close - MIN_LEFT)):
        a = asks(per, sec)
        if a is None:
            continue
        flow = sum3(signed, sec)
        if floor_on:
            normal = statistics.median([abs(sum3(signed, sec - 3 - k)) for k in range(0, 300, 3)]) or 1.0
        for side, sign in (("yes", 1), ("no", -1)):
            if not LO <= a[side] <= HI:
                continue
            if floor_on and not (sign * flow >= ratio * normal and sign * flow >= FLOORS[coin]):
                continue
            r = play(close, result, per, sec, side)
            if r:
                return sec, side, r
    return None


def summarize(label, picks):
    if not picks:
        print(f"{label:<28} 0 events")
        return
    p = [x[2][0] for x in picks]
    n = len(p)
    wonn = sum(1 for x in picks if x[3])
    mean = sum(p) / n
    se = (sum((v - mean) ** 2 for v in p) / n) ** 0.5 / math.sqrt(n) * 100
    order = sorted(range(n), key=lambda i: picks[i][4])
    a, b = [p[i] for i in order[: n // 2]], [p[i] for i in order[n // 2:]]
    avg = lambda xs: sum(xs) / len(xs) * 100 if xs else float("nan")  # noqa: E731
    print(f"{label:<28} {n:>5} events {wonn:>3} won ({100 * wonn / n:>3.0f}%) avg ask {sum(x[2][1] for x in picks) / n:.3f} | per ticket {mean * 100:>+7.1f}c +-{se:>5.1f} | older {avg(a):>+7.1f}c newer {avg(b):>+7.1f}c")


base = []
for mk in markets:
    f = find(mk, 0, False)
    if f:
        base.append((mk[0], f[1], f[2], mk[3] == f[1], mk[2]))
summarize("baseline (no surge)", base)
for ratio in RATIOS:
    picks = []
    for mk in markets:
        f = find(mk, ratio, True)
        if f:
            picks.append((mk[0], f[1], f[2], mk[3] == f[1], mk[2]))
    summarize(f"HL surge >= {ratio:g}x normal", picks)
print("DONE")
