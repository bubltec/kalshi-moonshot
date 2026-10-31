"""Resting-order ladder over the whole window: watch a side from the open; once its ask has been at or under ARM and then climbs to TRIGGER, a buy limit at LIMIT goes in; when it fills, a resting sell at TARGET goes in and stays until the close.
Replay on every crypto 15-minute window of the last DAYS days. Read-only.

Buy: from the trigger second the order rests for REST seconds (0 = the rest of the window up to LAST_LEFT s before the close). It fills the first second the side's ask is at or under LIMIT, at that ask (a limit never pays more than LIMIT; the book
gives the ask when it is lower). Sell: from the fill the order rests at TARGET and fills the first second the held bid is at TARGET or over, at TARGET. A buy or sale that never fills: an unfilled buy is no bet; a fill with no sale is held to the result (1 or 0).
Fees: BUYFEE=1 pays the taker fee on the buy, SELLFEE=1 pays it on the sale (a resting sell is a maker, so 0 is the lane's assumption). One buy per market, whichever side triggers first.

    scripts/cloud.sh run scripts/scalp_ladder_replay.py [DAYS=7] [ARM=0.15] [TRIGGER=0.20] [LIMIT=0.22] [TARGET=0.40]
    docker compose ... exec -T -e DAYS=3 bot python - < scripts/scalp_ladder_replay.py
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
ARM, TRIGGER, LIMIT, TARGET = (float(os.environ.get(k, v)) for k, v in (("ARM", "0.15"), ("TRIGGER", "0.20"), ("LIMIT", "0.22"), ("TARGET", "0.40")))
LAST_LEFT = 60
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
print(f"{len(markets)} crypto windows with a quote tape over {DAYS:g} days; arm {ARM:.2f}, trigger {TRIGGER:.2f}, buy limit {LIMIT:.2f}, sell {TARGET:.2f}\n")


def held(per, sec, side):
    v = per.get(sec)
    if v is None:
        return None
    return (v[0], v[1]) if side == "yes" else (1.0 - v[1], 1.0 - v[0])  # (held bid, held ask)


def triggers(close, per):
    """(sec, side) of the first side to be armed and then reach the trigger."""
    armed = {"yes": False, "no": False}
    for sec in range(int(close - 900), int(close) - LAST_LEFT):
        for side in ("yes", "no"):
            h = held(per, sec, side)
            if h is None:
                continue
            if h[1] <= ARM:
                armed[side] = True
            elif armed[side] and h[1] >= TRIGGER:
                return sec, side
    return None


def buy_fill(close, per, sec0, side, rest):
    end = int(close) - LAST_LEFT if rest == 0 else min(int(close) - LAST_LEFT, sec0 + rest)
    for sec in range(sec0 + 1, end):
        h = held(per, sec, side)
        if h is not None and h[1] <= LIMIT + 1e-9:
            return sec, h[1]
    return None


def sell_fill(close, per, sec1, side):
    for sec in range(sec1 + 1, int(close) - 3):
        h = held(per, sec, side)
        if h is not None and h[0] >= TARGET - 1e-9:
            return sec
    return None


picks = [(close, result, per, t) for close, result, per in markets for t in [triggers(close, per)] if t]
print(f"{len(picks)} windows trigger a buy order\n")
print(f"{'buy rests':>10} {'buyfee':>6} {'sellfee':>7} | {'filled':>6} {'sold':>5} {'held':>5} | {'per fill':>8} {'+-':>5} | {'sold avg':>8} {'held avg':>9} | {'older':>7} {'newer':>7}")
for rest in (20, 60, 0):
    fills = []
    for close, result, per, (sec0, side) in picks:
        f = buy_fill(close, per, sec0, side, rest)
        if f is None:
            continue
        sec1, ask = f
        sold = sell_fill(close, per, sec1, side)
        fills.append((close, result, side, ask, sold is not None))
    for buyfee, sellfee in ((1, 1), (1, 0), (0, 0)):
        rows = []
        for close, result, side, ask, sold in fills:
            cost = ask + (fee_per_contract(ask) if buyfee else 0.0)
            pnl = (TARGET - (fee_per_contract(TARGET) if sellfee else 0.0) - cost) if sold else ((1.0 if result == side else 0.0) - cost)
            rows.append((pnl, close, sold))
        if not rows:
            continue
        n = len(rows)
        p = [r[0] for r in rows]
        mean = sum(p) / n
        se = (sum((x - mean) ** 2 for x in p) / n) ** 0.5 / math.sqrt(n) * 100
        order = sorted(range(n), key=lambda i: rows[i][1])
        a, b = [p[i] for i in order[: n // 2]], [p[i] for i in order[n // 2:]]
        avg = lambda xs: sum(xs) / len(xs) * 100 if xs else float("nan")  # noqa: E731
        ns = sum(1 for r in rows if r[2])
        print(f"{('whole' if rest == 0 else str(rest) + ' s'):>10} {buyfee:>6} {sellfee:>7} | {n:>6} {ns:>5} {n - ns:>5} | {mean * 100:>+7.2f}c {se:>5.2f} | {avg([r[0] for r in rows if r[2]]):>+7.2f}c {avg([r[0] for r in rows if not r[2]]):>+8.2f}c | {avg(a):>+6.2f}c {avg(b):>+6.2f}c")
print("DONE")
