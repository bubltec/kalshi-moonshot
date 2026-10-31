"""What follows a sharp 3-second move on a crypto 15-minute market. Read-only.

For every BTC / ETH / SOL window of the last DAYS days, the per-second YES mid is walked. The first second the mid has moved MOVE or more within 3 s (and is at the far end of the move) is an event; the side that FELL is the one the move lane buys. One event per market.
The next AFTER seconds of the fallen side's bid are classified: "bounce" (it recovers at least half the fall), "flat" (it stays within 5c of the post-move level, no recovery) or "keeps falling" (it falls another 5c or more).
Then the lane's rules are replayed: a post-only buy BELOW cents under the ask at the event second fills only if the ask comes down to it within ARM s (no fee); the exit arms at UP over the price paid, sells at the bid DIP under the high or after PLATEAU s with no new high (taker fee); otherwise held to the result.

    docker compose ... exec -T -e DAYS=4 -e MOVE=0.30 bot python - < scripts/move_aftermath_study.py
    scripts/cloud.sh run scripts/move_aftermath_study.py
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

DAYS = float(os.environ.get("DAYS", "4"))
MOVES = [float(x) for x in os.environ.get("MOVE", "0.40,0.30,0.20").split(",")]
SPAN, AFTER, BELOW, ARM, UP, DIP, PLATEAU = 3, 60, 0.02, 10, 0.04, 0.03, 10
COINS = ("BTC", "ETH", "SOL")

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
    markets.append((w["ticker"], close, w["result"], per))
print(f"{len(markets)} BTC/ETH/SOL windows over {DAYS:g} days\n")


def side_px(per, sec, side):
    v = per.get(sec)
    if v is None:
        return None
    return (v[0], v[1]) if side == "yes" else (1.0 - v[1], 1.0 - v[0])  # (bid, ask)


def find(close, per, move):
    for sec in range(int(close - 900) + 30, int(close) - 90):
        now = per.get(sec)
        if now is None:
            continue
        mid = (now[0] + now[1]) / 2
        past = [(per[t][0] + per[t][1]) / 2 for t in range(sec - SPAN, sec) if t in per]
        if not past:
            continue
        if max(past) - mid >= move - 1e-9:
            return sec, "yes"
        if mid - min(past) >= move - 1e-9:
            return sec, "no"
    return None


def classify(per, sec, side):
    b0 = side_px(per, sec, side)
    if b0 is None:
        return None
    start = max((side_px(per, t, side) or (0, 0))[0] for t in range(sec - SPAN, sec)) or b0[0]
    path = [side_px(per, t, side)[0] for t in range(sec + 1, sec + AFTER + 1) if side_px(per, t, side)]
    if not path:
        return None
    fall = start - b0[0]
    if max(path) - b0[0] >= fall / 2:
        return "bounce"
    if b0[0] - min(path) >= 0.05:
        return "keeps falling"
    return "flat"


def lane(close, result, per, sec, side):
    """(filled, pnl per contract) of the lane's buy and exit; None when no fill."""
    a = side_px(per, sec, side)
    price = round(a[1] - BELOW, 2)
    if price <= 0.01:
        return None
    fill = next((t for t in range(sec + 1, sec + ARM + 1) if side_px(per, t, side) and side_px(per, t, side)[1] <= price + 1e-9), None)
    if fill is None:
        return None
    high, high_t, armed = 0.0, 0, False
    for t in range(fill + 1, int(close) - 3):
        p = side_px(per, t, side)
        if p is None:
            continue
        bid = p[0]
        if bid >= price + UP - 1e-9:
            armed = True
        if not armed:
            continue
        if bid > high + 1e-9 or high_t == 0:
            high_t = t
        high = max(high, bid)
        if high - bid >= DIP - 1e-9 or (t - high_t >= PLATEAU and bid >= price + UP - 1e-9):
            return bid - fee_per_contract(bid) - price
    return (1.0 if result == side else 0.0) - price


def stats(xs):
    n = len(xs)
    if not n:
        return "n=0"
    m = sum(xs) / n
    se = (sum((x - m) ** 2 for x in xs) / n) ** 0.5 / math.sqrt(n)
    return f"n={n:<4} avg {m * 100:>+6.1f}c +-{se * 100:.1f}"


for move in MOVES:
    ev = []
    for tk, close, result, per in markets:
        f = find(close, per, move)
        if f:
            ev.append((tk, close, result, per, f[0], f[1]))
    print(f"--- a {move * 100:.0f}c move in {SPAN}s: {len(ev)} markets ({100 * len(ev) / max(1, len(markets)):.1f}% of windows)")
    kinds = {}
    for tk, close, result, per, sec, side in ev:
        k = classify(per, sec, side)
        if k:
            kinds.setdefault(k, []).append((tk, close, result, per, sec, side))
    for k in ("bounce", "flat", "keeps falling"):
        rows = kinds.get(k, [])
        res = [r for r in (lane(c, rs, p, sec, sd) for _, c, rs, p, sec, sd in rows) if r is not None]
        held = [((1.0 if rs == sd else 0.0) - (side_px(p, sec, sd)[1])) for _, c, rs, p, sec, sd in rows if side_px(p, sec, sd)]
        print(f"  {k:<14} {len(rows):>4} events | lane fills {len(res):>3} ({100 * len(res) / max(1, len(rows)):.0f}%): {stats(res)} | taker buy held to result: {stats(held)}")
    allres = [r for r in (lane(c, rs, p, sec, sd) for tk, c, rs, p, sec, sd in ev) if r is not None]
    print(f"  all            {len(ev):>4} events | lane fills {len(allres):>3}: {stats(allres)}")
    print()
print("DONE")
