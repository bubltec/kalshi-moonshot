"""BUY A LOW SIDE WHEN IT STARTS RISING, NOT WHEN IT SITS IN A PRICE BAND: early_scalp buys a crypto 15-minute side whose ask is 14c-42c and not falling over 10 s. The idea tested here: a side that HAS BEEN low (its cheapest ask in the last
`LOOK` s is under LOW) and has now risen R over the last W seconds (5 or 10), at any price up to the cap, in minutes 0.5-9 of the window. Every crypto 15-minute window on the tape, both sides, the first trigger per side per window, bought at the ask ONE
second later (an order takes a moment) with the buy fee, then the lane's exits replayed second by second: follow the high once the bid is scalp_up over the price paid, sell on a dip of scalp_dip (3 s hold) or scalp_dip_hard at once, from minute 12 sell any gain, a
fall of cash_drop sells, and with 5 s left sell a hover; otherwise it is held to the result. Sales are priced at the bid one second after the rule fires, less the sale fee.
Reported per rule: triggers, win rate (sale or result above the price paid), mean net per contract, per dollar staked, and the standard error clustered by window. The first row is today's lane (band 14c-42c, 10 s not falling) for comparison.
Read-only. Places nothing.
    docker compose exec -T bot python - < scripts/early_rise_study.py   (SINCE_DAYS=7)
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
RULES = [("today: band 14-42c, 10 s not falling", "band", None, None, None, None)]
for LOW in (0.30, 0.42):
    for W in (5, 10):
        for R in (0.02, 0.03, 0.05):
            RULES.append((f"been under {LOW:.2f}, up {R:.2f} in {W}s", "rise", LOW, W, R, 60))


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


res = defaultdict(list)  # rule -> [(window, net per contract, paid incl fee)]
wins = main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE series IN ('KXBTC15M','KXETH15M','KXSOL15M','KXXRP15M','KXDOGE15M') AND result IN ('yes','no') AND close_ts > extract(epoch from now()) - ?*86400", (days,)).fetchall()
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
        done = set()
        for sec in range(int(t0 + 30), int(t0 + 540)):
            cur = side_px(g, sec, side)
            if cur is None or len(done) == len(RULES):
                continue
            ask = cur[1]
            for ri, (name, kind, LOW, W, R, LOOK) in enumerate(RULES):
                if ri in done:
                    continue
                if kind == "band":
                    prev = side_px(g, sec - 10, side)
                    ok = 0.14 <= ask <= 0.42 and prev is not None and ask >= prev[1] - 1e-9 and (sec - t0) >= 30
                else:
                    prev = side_px(g, sec - W, side)
                    lows = [p[1] for p in (side_px(g, sec - k, side) for k in range(LOOK + 1)) if p is not None]
                    ok = prev is not None and bool(lows) and ask <= CAP and min(lows) < LOW and ask - prev[1] >= R - 1e-9
                if not ok:
                    continue
                done.add(ri)
                fill = side_px(g, sec + 1, side)
                if fill is None or not (0.05 <= fill[1] <= CAP):
                    continue
                paid = fill[1]
                got = exit_value(g, side, sec + 1, paid, close, won)
                res[name].append((close, got - paid - fee(paid), paid + fee(paid)))
print(f"{n} windows, {days:.0f} days, both sides, first trigger per side per window\n")
print(f"{'rule':40} {'n':>5} {'win%':>5} {'net/contract':>13} {'per $ staked':>13} {'+-se (per $)':>12}")
for name, *_ in RULES:
    r = res.get(name, [])
    if len(r) < 20:
        print(f"{name:40} {len(r):>5}")
        continue
    wins_ = sum(1 for x in r if x[1] > 0) / len(r)
    per = [x[1] / x[2] for x in r]
    by = defaultdict(float)
    for x, p in zip(r, per, strict=True):
        by[x[0]] += p
    se = math.sqrt(len(by)) * statistics.pstdev(list(by.values())) / len(r) if len(by) > 1 else float("nan")
    print(f"{name:40} {len(r):>5} {100 * wins_:>4.0f}% {100 * statistics.mean(x[1] for x in r):>+12.1f}c {100 * statistics.mean(per):>+12.1f}% {100 * se:>11.1f}%")
