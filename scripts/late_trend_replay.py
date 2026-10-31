"""Trade the last 3 minutes of a crypto 15-minute market by the shape of the 3 minutes before. Read-only.

History is minutes 9-12 of the window (per-second YES mid); the trade window is minutes 12-15. The shape is
  up    : the mid rose at least MOVE over the history and the path is a trend (net move / total movement >= EFF), so a push, not a chop
  down  : the mirror
  mixed : anything else -> pass.
Up buys YES, down buys NO. LEVEL=1 reads "20c over target" as the mid sitting MOVE over (or under) 50c at minute 12 instead of as a move over the history.
The buy is at the ask ENTRY seconds after minute 12 plus the taker fee (or BID=1: a resting bid at the bid, no fee, filled only if the ask comes down to it within 20 s), held to the result.
Shown per class (up / down / mixed pass-through) with the share of windows, the average price paid, the win rate, and the result per contract with its error and older/newer halves.

    docker compose -p moonshot-local --env-file .env.local exec -T -e DAYS=8 -e MOVE=0.20 bot python - < scripts/late_trend_replay.py
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
DAYS = float(os.environ.get("DAYS", "8"))
MOVES = [float(x) for x in os.environ.get("MOVE", "0.10,0.20,0.30").split(",")]
EFF = float(os.environ.get("EFF", "0.5"))
LEVEL = os.environ.get("LEVEL", "0") == "1"
BID = os.environ.get("BID", "0") == "1"
ENTRY = int(os.environ.get("ENTRY", "2"))
COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")

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
print(f"{len(markets)} crypto windows over {DAYS:g} days; entry {ENTRY}s after minute 12, {'resting bid' if BID else 'taker at the ask'}, {'level' if LEVEL else 'move'} reading, efficiency >= {EFF}\n")


def shape(close, per, move):
    t0, t1 = int(close - 360), int(close - 180)  # minutes 9 and 12
    pts = [(per[t][0] + per[t][1]) / 2 for t in range(t0, t1 + 1) if t in per]
    if len(pts) < 120 or t1 not in per:
        return None
    net = pts[-1] - pts[0]
    total = sum(abs(b - a) for a, b in zip(pts, pts[1:])) or 1e-9
    if LEVEL:
        lvl = pts[-1] - 0.5
        return "up" if lvl >= move - 1e-9 else "down" if lvl <= -move + 1e-9 else "mixed"
    if abs(net) >= move - 1e-9 and abs(net) / total >= EFF:
        return "up" if net > 0 else "down"
    return "mixed"


def trade(close, result, per, side):
    sec = int(close - 180) + ENTRY
    v = per.get(sec)
    if v is None:
        return None
    ask = v[1] if side == "yes" else 1.0 - v[0]
    if BID:
        px = v[0] if side == "yes" else 1.0 - v[1]
        if px <= 0.01:
            return None
        for t in range(sec + 1, sec + 21):
            u = per.get(t)
            if u is not None and (u[1] if side == "yes" else 1.0 - u[0]) <= px + 1e-9:
                return (1.0 if result == side else 0.0) - px, px
        return None
    if ask >= 0.99 or ask <= 0.01:
        return None
    return (1.0 if result == side else 0.0) - ask - fee_per_contract(ask), ask


def report(label, rows, total):
    n = len(rows)
    if not n:
        print(f"  {label:<6} 0 trades")
        return
    p = [r[0] for r in rows]
    mean = sum(p) / n
    se = (sum((x - mean) ** 2 for x in p) / n) ** 0.5 / math.sqrt(n) * 100
    half = n // 2
    old, new = p[:half], p[half:]
    avg = lambda xs: sum(xs) / len(xs) * 100 if xs else float("nan")  # noqa: E731
    print(f"  {label:<6} {n:>4} trades ({100 * n / total:>3.0f}% of windows) | paid {sum(r[1] for r in rows) / n:.2f} | won {100 * sum(1 for r in rows if r[2]) / n:>3.0f}% | {mean * 100:>+6.1f}c +-{se:4.1f} | older {avg(old):>+6.1f}c newer {avg(new):>+6.1f}c")


for move in MOVES:
    print(f"MOVE {move * 100:.0f}c")
    cls = {"up": [], "down": [], "mixed": 0}
    for close, result, per in markets:
        sh = shape(close, per, move)
        if sh is None:
            continue
        if sh == "mixed":
            cls["mixed"] += 1
            continue
        side = "yes" if sh == "up" else "no"
        r = trade(close, result, per, side)
        if r:
            cls[sh].append((r[0], r[1], result == side, close))
    total = len(cls["up"]) + len(cls["down"]) + cls["mixed"]
    for k in ("up", "down"):
        cls[k].sort(key=lambda r: r[3])
        report(k, cls[k], max(total, 1))
    both = sorted(cls["up"] + cls["down"], key=lambda r: r[3])
    report("both", both, max(total, 1))
    print(f"  mixed  {cls['mixed']:>4} windows passed")
    print()
print("DONE")
