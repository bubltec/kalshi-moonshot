"""Buy a sharp dip through a price: a side whose ask falls fast down to LEVEL is bought, and a resting sell at TARGET waits to the close. Replay on every crypto 15-minute window of the last DAYS days. Read-only.

The trigger is the first second a side's ask is at or under LEVEL (and at or over FLOOR: a side already gone is not a dip) while its ask LOOKBACK seconds earlier was at least DROP over the ask now, so the price came down hard. One buy per market, whichever side
triggers first, between minute FROM and LAST_LEFT s before the close. The buy is at the ask one second later (LAT) with the taker fee. The sale rests at TARGET and fills at TARGET the first second the held bid is at TARGET or over (maker: no fee; SELLFEE=1 charges it).
A STOP of S sells at the bid one second after the bid is S under the price paid. Otherwise held to the result (1 or 0).

    scripts/cloud.sh run scripts/scalp_dip_entry_replay.py [DAYS=7] [LEVEL=0.20] [FLOOR=0.10] [FROM=1] [SELLFEE=0]
    docker compose ... exec -T -e DAYS=3 bot python - < scripts/scalp_dip_entry_replay.py
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
LEVEL, FLOOR, FROM = float(os.environ.get("LEVEL", "0.20")), float(os.environ.get("FLOOR", "0.10")), float(os.environ.get("FROM", "1"))
SELLFEE = os.environ.get("SELLFEE", "0") == "1"
LAT, LAST_LEFT = 1, 60
COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")
SPEEDS = [(30, 0.08), (30, 0.12), (60, 0.10), (60, 0.15), (60, 0.20)]
TARGETS = (0.30, 0.35, 0.40)
STOPS = (None, 0.05, 0.08)

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
print(f"{len(markets)} crypto windows over {DAYS:g} days; dip to {LEVEL:.2f} (not under {FLOOR:.2f}), sale fee {'on' if SELLFEE else 'off (maker)'}\n")


def held(per, sec, side):
    v = per.get(sec)
    if v is None:
        return None
    return (v[0], v[1]) if side == "yes" else (1.0 - v[1], 1.0 - v[0])  # (held bid, held ask)


def trigger(close, per, lookback, drop):
    for sec in range(int(close - 900 + FROM * 60), int(close) - LAST_LEFT):
        for side in ("yes", "no"):
            now, before = held(per, sec, side), held(per, sec - lookback, side)
            if now is None or before is None:
                continue
            if FLOOR <= now[1] <= LEVEL and before[1] - now[1] >= drop - 1e-9:
                f = held(per, sec + LAT, side)
                if f is not None and f[1] <= LEVEL + 0.03:
                    return sec + LAT, side, f[1]
    return None


def play(close, result, per, sec0, side, ask, target, stop):
    cost = ask + fee_per_contract(ask)
    for sec in range(sec0 + 1, int(close) - 3):
        h = held(per, sec, side)
        if h is None:
            continue
        if h[0] >= target - 1e-9:
            return target - (fee_per_contract(target) if SELLFEE else 0.0) - cost, True
        if stop is not None and h[0] <= ask - stop + 1e-9:
            f = held(per, sec + LAT, side) or h
            return f[0] - fee_per_contract(f[0]) - cost, False
    return (1.0 if result == side else 0.0) - cost, False


def avg(xs):
    return sum(xs) / len(xs) * 100 if xs else float("nan")


print(f"{'dip':>12} {'target':>6} {'stop':>5} | {'bets':>4} {'sold':>5} | {'per bet':>8} {'+-':>5} | {'older':>7} {'newer':>7}")
for lookback, drop in SPEEDS:
    picks = [(close, result, per, t) for close, result, per in markets for t in [trigger(close, per, lookback, drop)] if t]
    for target in TARGETS:
        for stop in STOPS:
            res = [(play(close, result, per, t[0], t[1], t[2], target, stop), close) for close, result, per, t in picks]
            if not res:
                continue
            p = [r[0][0] for r in res]
            n = len(p)
            mean = sum(p) / n
            se = (sum((x - mean) ** 2 for x in p) / n) ** 0.5 / math.sqrt(n) * 100
            order = sorted(range(n), key=lambda i: res[i][1])
            a, b = [p[i] for i in order[: n // 2]], [p[i] for i in order[n // 2:]]
            print(f"{drop * 100:>3.0f}c/{lookback:>3}s {target:>6.2f} {('-' if stop is None else format(stop, '.2f')):>5} | {n:>4} {100 * sum(1 for r in res if r[0][1]) / n:>4.0f}% | {mean * 100:>+7.2f}c {se:>5.2f} | {avg(a):>+6.2f}c {avg(b):>+6.2f}c")
print("DONE")
