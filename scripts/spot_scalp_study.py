"""DOES THE SPOT PRICE TELL A GOOD LOW-END SCALP FROM A BAD ONE: early_scalp buys a crypto 15-minute side at 14c-42c (not falling over 10 s) and sells the bounce. Every such entry on the tape (first per side per window, bought at the ask one second later,
exits and fees as in early_rise_study.py) is tagged with what the Coinbase spot price was doing at the moment of the buy, and the results are grouped by it:
  z    how far spot is from the window's strike on THIS side's winning direction, in units of the window's own noise (the standard deviation of 1 s spot moves over the last 5 minutes, times the square root of the seconds left). Positive = spot is on this side's winning side.
  move the spot's change over the last 5 s and 10 s in this side's direction (positive = spot moving the way this side pays), in the same noise units per second.
Reported per group: entries, win rate, net per contract AFTER the fees, net per contract BEFORE them (is there any gross edge on the low end), and per dollar staked with the window-clustered standard error. Read-only.
    docker compose exec -T bot python - < scripts/spot_scalp_study.py   (SINCE_DAYS=7)
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



COIN = {"KXBTC15M": "coinbase:BTC-USD", "KXETH15M": "coinbase:ETH-USD", "KXSOL15M": "coinbase:SOL-USD", "KXXRP15M": "coinbase:XRP-USD", "KXDOGE15M": "coinbase:DOGE-USD"}


def spot_grid(symbol, t0, t1):
    q = tape.execute("SELECT ts, px FROM feed_ticks WHERE symbol=? AND ts BETWEEN ? AND ? ORDER BY ts", (symbol, t0 - 330, t1)).fetchall()
    ts = [float(r[0]) for r in q]
    out = {}
    for sec in range(int(t0) - 320, int(t1) + 1):
        i = bisect.bisect_right(ts, sec + 0.999) - 1
        if i >= 0 and sec + 0.999 - ts[i] <= 10:
            out[sec] = float(q[i][1])
    return out


rows = []  # (window, side, z, move5, move10, net_after_fees, net_before_fees, stake)
wins = main.db.execute("SELECT ticker, series, close_ts, strike, result FROM windows WHERE series IN ('KXBTC15M','KXETH15M','KXSOL15M','KXXRP15M','KXDOGE15M') AND result IN ('yes','no') AND strike > 0 AND close_ts > extract(epoch from now()) - ?*86400", (days,)).fetchall()
n = 0
for w in wins:
    close, strike = float(w["close_ts"]), float(w["strike"])
    t0 = close - 900
    g = grid(w["ticker"], t0, close)
    sp = spot_grid(COIN[w["series"]], t0, close)
    if len(g) < 300 or len(sp) < 400:
        continue
    n += 1
    for side in ("yes", "no"):
        won = w["result"] == side
        sign = 1.0 if side == "yes" else -1.0
        for sec in range(int(t0 + 30), int(t0 + 540)):
            cur, prev = side_px(g, sec, side), side_px(g, sec - 10, side)
            if cur is None or prev is None or not (0.14 <= cur[1] <= 0.42 and cur[1] >= prev[1] - 1e-9):
                continue
            fill = side_px(g, sec + 1, side)
            if fill is None or not (0.05 <= fill[1] <= CAP):
                break
            moves = [sp[k] - sp[k - 1] for k in range(sec - 300, sec + 1) if k in sp and k - 1 in sp]
            if len(moves) < 100 or sec not in sp or sec - 5 not in sp or sec - 10 not in sp:
                break
            sig = statistics.pstdev(moves)
            if sig <= 0:
                break
            left = close - sec
            z = sign * (sp[sec] - strike) / (sig * math.sqrt(left))
            m5 = sign * (sp[sec] - sp[sec - 5]) / (sig * math.sqrt(5))
            m10 = sign * (sp[sec] - sp[sec - 10]) / (sig * math.sqrt(10))
            paid = fill[1]
            got = exit_value(g, side, sec + 1, paid, close, won)
            fees = fee(paid) + (fee(got) if 0 < got < 1 else 0.0)
            rows.append((close, side, z, m5, m10, got - paid - fee(paid), got - paid + (fee(got) if 0 < got < 1 else 0.0), paid + fee(paid), fees))
            break
print(f"{n} windows, {days:.0f} days, {len(rows)} entries (today's band, first per side per window)\n")


def show(title, groups):
    print(title)
    print(f"  {'group':34} {'n':>5} {'win%':>5} {'after fees':>11} {'before fees':>12} {'per $ staked':>13} {'+-se':>6}")
    for name, pick in groups:
        r = [x for x in rows if pick(x)]
        if len(r) < 30:
            print(f"  {name:34} {len(r):>5}")
            continue
        per = [x[5] / x[7] for x in r]
        by = defaultdict(float)
        for x, p in zip(r, per, strict=True):
            by[x[0]] += p
        se = math.sqrt(len(by)) * statistics.pstdev(list(by.values())) / len(r)
        print(f"  {name:34} {len(r):>5} {100 * sum(1 for x in r if x[5] > 0) / len(r):>4.0f}% {100 * statistics.mean(x[5] for x in r):>+10.1f}c {100 * statistics.mean(x[6] for x in r):>+11.1f}c {100 * statistics.mean(per):>+12.1f}% {100 * se:>5.1f}%")


show("all entries", [("all", lambda x: True)])
show("by z (spot against the strike, in noise units; + = on this side's winning side)", [
    ("z < -1", lambda x: x[2] < -1), ("-1 <= z < -0.5", lambda x: -1 <= x[2] < -0.5), ("-0.5 <= z < 0", lambda x: -0.5 <= x[2] < 0),
    ("0 <= z < 0.5", lambda x: 0 <= x[2] < 0.5), ("0.5 <= z < 1", lambda x: 0.5 <= x[2] < 1), ("z >= 1", lambda x: x[2] >= 1)])
show("by spot move over the last 5 s (noise units, + = moving the way this side pays)", [
    ("move5 < -1", lambda x: x[3] < -1), ("-1 <= move5 < 0", lambda x: -1 <= x[3] < 0), ("0 <= move5 < 1", lambda x: 0 <= x[3] < 1), ("move5 >= 1", lambda x: x[3] >= 1)])
show("by spot move over the last 10 s", [
    ("move10 < -1", lambda x: x[4] < -1), ("-1 <= move10 < 0", lambda x: -1 <= x[4] < 0), ("0 <= move10 < 1", lambda x: 0 <= x[4] < 1), ("move10 >= 1", lambda x: x[4] >= 1)])
show("combined", [
    ("z >= 0 and move5 > 0", lambda x: x[2] >= 0 and x[3] > 0), ("z >= -0.5 and move5 > 0 and move10 > 0", lambda x: x[2] >= -0.5 and x[3] > 0 and x[4] > 0),
    ("z < 0 and move5 > 0", lambda x: x[2] < 0 and x[3] > 0), ("z >= 0.5 and move5 >= 1", lambda x: x[2] >= 0.5 and x[3] >= 1), ("move5 <= 0 or z < -0.5", lambda x: x[3] <= 0 or x[2] < -0.5)])
