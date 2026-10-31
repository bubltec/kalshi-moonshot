"""Replay the early scalp over a price band and a gain, on every crypto 15-minute window of the last DAYS days. Read-only.

A side is bought at its ask (taker fee paid; FEE=0 for the fee-free resting buy) the first second, between minute ENTRY_FROM and ENTRY_TO of the window, that its ask is inside the band and has not fallen over the last 10 s,
one buy per market. The exit is then, by rule:
    take N      sell at the bid LAT seconds after the held bid first reaches the price paid plus N (a flat target); fee on the sale
    follow N    once the bid reaches the price paid plus N, follow its high and sell LAT seconds after it falls DIP under it (the lane's scalp_up / scalp_dip, no hold)
A position never sold is held to the settlement result, or sold at the bid when 5 s are left if LEFT_SELL=1. "loss kept" is the average result of the positions that never hit the exit.

    scripts/cloud.sh run scripts/scalp_range_replay.py [DAYS=7] [LAT=1] [FEE=1] [ENTRY_FROM=1] [ENTRY_TO=9]
"""
import bisect
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
LAT = int(os.environ.get("LAT", "1"))
FEE = os.environ.get("FEE", "1") == "1"
ENTRY_FROM, ENTRY_TO = float(os.environ.get("ENTRY_FROM", "1")), float(os.environ.get("ENTRY_TO", "9"))
DIP = 0.04
BANDS = [(0.14, 0.42), (0.20, 0.42), (0.40, 0.50), (0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.55, 0.65)]
RULES = [("take", 0.06), ("take", 0.10), ("follow", 0.06), ("follow", 0.10)]
COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE")


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
    per_sec = {}
    for sec in range(int(close - 900), int(close) + 1):
        i = bisect.bisect_right(ts, sec + 0.999) - 1
        if i >= 0 and sec + 0.999 - ts[i] <= 15:
            per_sec[sec] = (float(q[i][1]), float(q[i][2]))
    markets.append((w["ticker"], m.group(1), close, w["result"], per_sec))
print(f"{len(markets)} crypto windows with a quote tape over {DAYS:g} days\n")


def held(per, sec, side):
    v = per.get(sec)
    if v is None:
        return None
    bid, ask = v
    return (bid, ask) if side == "yes" else (1.0 - ask, 1.0 - bid)  # (held bid, held ask)


def entries(per, close, lo, hi):
    """(sec, side, ask) of the first second a side's ask is in the band and not lower than 10 s ago."""
    for sec in range(int(close - 900 + ENTRY_FROM * 60), int(close - 900 + ENTRY_TO * 60)):
        for side in ("yes", "no"):
            now, before = held(per, sec, side), held(per, sec - 10, side)
            if now is None or before is None:
                continue
            if lo <= now[1] <= hi and now[1] >= before[1] - 1e-9:
                return sec, side, now[1]
    return None


def play(per, close, result, sec0, side, ask, rule, n):
    """(pnl per contract, how)."""
    cost = ask + fee(ask)
    high, arm = 0.0, ask + n
    for sec in range(sec0 + 1, int(close) - 5):
        h = held(per, sec, side)
        if h is None:
            continue
        bid = h[0]
        if rule == "take" and bid >= arm - 1e-9:
            f = held(per, sec + LAT, side) or h
            return f[0] - fee(f[0]) - cost, "target"
        if rule == "follow":
            if bid >= arm - 1e-9:
                high = max(high, bid)
            if high and bid <= high - DIP + 1e-9:
                f = held(per, sec + LAT, side) or h
                return f[0] - fee(f[0]) - cost, "target"
    return (1.0 if result == side else 0.0) - cost, "kept"


print(f"fee on buys and sells: {'yes' if FEE else 'no'}; entry minutes {ENTRY_FROM:g}-{ENTRY_TO:g}; sale {LAT} s after the trigger\n")
print(f"{'band':>11} {'rule':>11} | {'bets':>5} {'hit exit':>8} {'avg ask':>7} | {'per bet':>8} {'hit avg':>8} {'kept avg':>9} | {'older half':>10} {'newer half':>10}")
for lo, hi in BANDS:
    picks = []
    for ticker, coin, close, result, per in markets:
        e = entries(per, close, lo, hi)
        if e:
            picks.append((ticker, coin, close, result, per, e))
    for rule, n in RULES:
        res = [(play(per, close, result, e[0], e[1], e[2], rule, n), close, e[2]) for _t, _c, close, result, per, e in picks]
        if not res:
            continue
        pn = [r[0][0] for r in res]
        hit = [r[0][0] for r in res if r[0][1] == "target"]
        kept = [r[0][0] for r in res if r[0][1] == "kept"]
        order = sorted(range(len(res)), key=lambda i: res[i][1])
        h = len(order) // 2
        a, b = [pn[i] for i in order[:h]], [pn[i] for i in order[h:]]
        avg = lambda xs: (sum(xs) / len(xs) * 100) if xs else float("nan")  # noqa: E731
        print(f"{lo:.2f}-{hi:.2f} {rule + ' ' + format(n, '.2f'):>11} | {len(res):>5} {100 * len(hit) / len(res):>7.0f}% {avg([r[2] for r in res]) / 100:>7.3f} | {avg(pn):>+7.2f}c {avg(hit):>+7.2f}c {avg(kept):>+8.2f}c | {avg(a):>+9.2f}c {avg(b):>+9.2f}c")
print("DONE")
