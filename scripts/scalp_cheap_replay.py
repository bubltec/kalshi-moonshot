"""Replay a cheap-side scalp over recorded 15-minute windows. Places nothing.

Buy a side whose ask is in [PRICE_LO, PRICE_HI] while the window is between MIN_ELAPSED and MAX_ELAPSED minutes old and the side's own mid has moved
TREND cents over the last TREND_SECONDS (negative = the side is getting cheaper, i.e. the market is moving away from the strike against it; positive = a bounce).
The buy fills at the ask one second after the signal. It then sells at the bid one second after the bid reaches fill + TARGET, or at EXIT_LEFT seconds left, or holds to the close
(EXIT_LEFT = 0). One bet per window. Fees: the sale pays the taker fee; the buy is taken at the ask and pays it too.

    scripts/cloud.sh run scripts/scalp_cheap_replay.py        # treasuries, all recorded days
    SERIES=KXGOLD15M,KXSILVER15M DAYS=7 scripts/cloud.sh run scripts/scalp_cheap_replay.py
TARGET_MODE=mult reads TARGETS as multiples of the fill (1.0 = sell at double); TRENDS=9 turns the trend rule off.
Prints one line per (trend rule, target, exit) with bets, win rate, net dollars on a $STAKE ticket, and how many ended as total losses.
"""
import bisect
import itertools
import os
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

SERIES = os.environ.get("SERIES", "KX2YRRATE15M,KX5YRRATE15M,KX10YRRATE15M,KX30YRRATE15M").split(",")
DAYS = float(os.environ.get("DAYS", "30"))
PRICE_LO, PRICE_HI = float(os.environ.get("PRICE_LO", "0.20")), float(os.environ.get("PRICE_HI", "0.40"))
MIN_ELAPSED, MAX_ELAPSED = float(os.environ.get("MIN_ELAPSED", "1")), float(os.environ.get("MAX_ELAPSED", "14"))
TREND_SECONDS = float(os.environ.get("TREND_SECONDS", "60"))
STAKE = float(os.environ.get("STAKE", "10"))
TRENDS = [float(x) for x in os.environ.get("TRENDS", "-0.10,-0.05,0,0.05,0.10").split(",")]
TARGETS = [float(x) for x in os.environ.get("TARGETS", "0.10,0.15,0.20").split(",")]
MULT = os.environ.get("TARGET_MODE") == "mult"  # targets are a share of the fill price (1.0 = sell at double) instead of cents
EXITS = [float(x) for x in os.environ.get("EXITS", "0,60,120").split(",")]

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
now = time.time()
windows = [w for w in main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE close_ts BETWEEN ? AND ? AND result IN ('yes','no')", (now - DAYS * 86400, now - 1800)).fetchall() if w["series"] in SERIES]


def load(w):
    close = w["close_ts"]
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (w["ticker"], close - 900, close)).fetchall()
    return close, [float(x[0]) for x in q], [float(x[1]) for x in q], [float(x[2]) for x in q]


def _mid(yb, ya, k, side):
    m = (yb[k] + ya[k]) / 2
    return m if side == "yes" else 1 - m


def run(data, trend, target, exit_left):
    pnl = []
    for w, (close, ts, yb, ya) in data:
        opened = close - 900
        last = -99.0
        for i, t in enumerate(ts):
            el = (t - opened) / 60
            if el < MIN_ELAPSED or el > MAX_ELAPSED or t - last < 2:
                continue
            last = t
            j = bisect.bisect_right(ts, t - TREND_SECONDS) - 1
            if j < 0:
                continue
            for side in ("yes", "no"):
                ask = ya[i] if side == "yes" else 1 - yb[i]
                if not PRICE_LO <= ask <= PRICE_HI:
                    continue
                move = _mid(yb, ya, i, side) - _mid(yb, ya, j, side)
                if trend >= 9:
                    pass  # TRENDS=9: no trend rule
                elif (trend < 0 and move > trend) or (trend > 0 and move < trend) or (trend == 0 and abs(move) > 0.02):
                    continue
                k = min(bisect.bisect_left(ts, t + 1), len(ts) - 1)
                fill = ya[k] if side == "yes" else 1 - yb[k]
                if not PRICE_LO <= fill <= PRICE_HI + 0.03:
                    continue
                n = STAKE / fill
                out = None
                for u in range(k, len(ts)):
                    bid = yb[u] if side == "yes" else 1 - ya[u]
                    if bid >= fill + (fill * target if MULT else target):
                        v = min(bisect.bisect_left(ts, ts[u] + 1), len(ts) - 1)
                        px = yb[v] if side == "yes" else 1 - ya[v]
                        out = (px - fill) * n - fee(px) * n - fee(fill) * n
                        break
                    if exit_left and close - ts[u] <= exit_left:
                        out = (bid - fill) * n - fee(bid) * n - fee(fill) * n
                        break
                if out is None:
                    out = ((1.0 - fill) * n if w["result"] == side else -fill * n) - fee(fill) * n
                pnl.append(out)
                break
            else:
                continue
            break
    return pnl


data = [(w, load(w)) for w in windows]
data = [(w, d) for w, d in data if len(d[1]) >= 30]
print(f"{len(windows)} settled windows in {SERIES}, {len(data)} with quotes; price {PRICE_LO:.2f}-{PRICE_HI:.2f}, minutes {MIN_ELAPSED:g}-{MAX_ELAPSED:g}, ${STAKE:g} ticket")
print(f"{'trend':>7} {'target':>7} {'exit_left':>9} {'bets':>5} {'win%':>5} {'net$':>8} {'total losses':>12}")
for trend, target, ex in itertools.product(TRENDS, TARGETS, EXITS):
    p = run(data, trend, target, ex)
    if p:
        print(f"{trend:>+7.2f} {target:>7.2f} {ex:>9g} {len(p):>5} {sum(x > 0 for x in p) / len(p) * 100:>5.0f} {sum(p):>8.1f} {sum(x < -0.9 * STAKE for x in p):>12}")
print("DONE")
