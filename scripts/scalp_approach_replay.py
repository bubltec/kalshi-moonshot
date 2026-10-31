"""Approach entry: watch a side once its ask is at or under ARM, buy it when the ask climbs up through ENTRY, sell when the bid reaches TARGET. Replay on every crypto 15-minute window of the last DAYS days. Read-only.

A side is armed the first second its ask is at or under ARM (inside minutes FROM-TO of the window). Once armed, the first second its ask is at or over ENTRY (and not more than SLIP over it: a jump past the line is skipped, the fill would not be near the price) is the buy, at the ask,
with the taker fee (FEE=0 for the fee-free resting buy). One buy per market, whichever side gets there first. The exit is the first second the held bid is at TARGET or over: sold 1 s later at the bid, fee paid. A STOP of S sells 1 s after the bid falls S under the price paid.
A position that hits neither is held to the result (1 or 0). Rows are by ARM, TARGET and STOP; "kept" is the share that hit neither; older / newer are the halves by time.

    scripts/cloud.sh run scripts/scalp_approach_replay.py [DAYS=7] [ENTRY=0.20] [SLIP=0.03] [FROM=0.5] [TO=12] [FEE=1]
    docker compose ... exec -T -e DAYS=3 bot python - < scripts/scalp_approach_replay.py
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
ENTRY, SLIP = float(os.environ.get("ENTRY", "0.20")), float(os.environ.get("SLIP", "0.03"))
FROM, TO = float(os.environ.get("FROM", "0.5")), float(os.environ.get("TO", "12"))
FEE = os.environ.get("FEE", "1") == "1"
LAT = 1
COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")
ARMS = (0.15, 0.10)
TARGETS = (0.30, 0.35, 0.40, 0.50)
STOPS = (None, 0.05, 0.08, 0.12)


def fee(p):
    return fee_per_contract(p) if FEE else 0.0


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
print(f"{len(markets)} crypto windows with a quote tape over {DAYS:g} days; entry at {ENTRY:.2f} (at most {SLIP:.2f} over), minutes {FROM:g}-{TO:g}, fee {'on' if FEE else 'off'}\n")


def held(per, sec, side):
    v = per.get(sec)
    if v is None:
        return None
    return (v[0], v[1]) if side == "yes" else (1.0 - v[1], 1.0 - v[0])  # (held bid, held ask)


def find_entry(close, per, arm):
    armed = {"yes": False, "no": False}
    for sec in range(int(close - 900 + FROM * 60), int(close - 900 + TO * 60)):
        for side in ("yes", "no"):
            h = held(per, sec, side)
            if h is None:
                continue
            if h[1] <= arm:
                armed[side] = True
            elif armed[side] and h[1] >= ENTRY:
                if h[1] <= ENTRY + SLIP:
                    return sec, side, h[1]
                armed[side] = False  # jumped past the line: not this one
    return None


def play(close, result, per, sec0, side, ask, target, stop):
    cost = ask + fee(ask)
    for sec in range(sec0 + 1, int(close) - 5):
        h = held(per, sec, side)
        if h is None:
            continue
        if h[0] >= target - 1e-9 or (stop is not None and h[0] <= ask - stop + 1e-9):
            f = held(per, sec + LAT, side) or h
            return f[0] - fee(f[0]) - cost, True
    return (1.0 if result == side else 0.0) - cost, False


def avg(xs):
    return sum(xs) / len(xs) * 100 if xs else float("nan")


print(f"{'arm':>5} {'target':>6} {'stop':>5} | {'bets':>4} {'hit':>4} {'kept':>5} | {'per bet':>8} {'+-':>5} | {'older':>7} {'newer':>7}")
for arm in ARMS:
    picks = [(close, result, per, e) for close, result, per in markets for e in [find_entry(close, per, arm)] if e]
    for target in TARGETS:
        for stop in STOPS:
            res = [(play(close, result, per, e[0], e[1], e[2], target, stop), close) for close, result, per, e in picks]
            if not res:
                continue
            pn = [r[0][0] for r in res]
            n = len(pn)
            mean = sum(pn) / n
            se = (sum((x - mean) ** 2 for x in pn) / n) ** 0.5 / math.sqrt(n) * 100
            order = sorted(range(n), key=lambda i: res[i][1])
            a, b = [pn[i] for i in order[: n // 2]], [pn[i] for i in order[n // 2:]]
            hit = sum(1 for r in res if r[0][1])
            print(f"{arm:>5.2f} {target:>6.2f} {('-' if stop is None else format(stop, '.2f')):>5} | {n:>4} {100 * hit / n:>3.0f}% {100 * (n - hit) / n:>4.0f}% | {mean * 100:>+7.2f}c {se:>5.2f} | {avg(a):>+6.2f}c {avg(b):>+6.2f}c")
print("DONE")
