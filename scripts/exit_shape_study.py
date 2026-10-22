"""Exit shapes on recorded 15-minute ticks: how much of a run does each trail keep? Replays ticks; places nothing.

A bet is entered at the ask for a side at minutes 4-12 of the window inside a price band, then exited by a trail: armed once a peak
that held 3 s is `act` over the price paid; sold when the bid is `drop` under that peak for 10 s, or `fast` under it at once. Variants:
a drop that grows with the run (`k` of the peak's gain), a higher arming gain, and a minimum profit (below it the position is held).
Every bet that never exits is held to settlement. ROI is after fees and a 2-cent sale cushion.
    python scripts/exit_shape_study.py data/moonshot.db data/tape.db [low high [minutes, e.g. 10,12,13,14]]
"""
import bisect
import sqlite3
import statistics as st
import sys

from kalshi_moonshot.strategy import fee_per_contract as fee

db = sqlite3.connect(sys.argv[1])
tape = sqlite3.connect(sys.argv[2])
LO, HI = (float(sys.argv[3]), float(sys.argv[4])) if len(sys.argv) > 4 else (0.10, 0.39)
wins = db.execute("select ticker, close_ts, result from windows where result in ('yes','no') and series like '%15M' order by close_ts").fetchall()
cache: dict = {}
SLIP = 0.02
MINUTES = (4, 6, 8, 10, 12) if len(sys.argv) <= 5 else tuple(int(x) for x in sys.argv[5].split(','))


def quotes(tk):
    if tk not in cache:
        r = tape.execute("select ts,bid,ask from quotes where ticker=? and bid is not null and ask is not null order by ts", (tk,)).fetchall()
        cache[tk] = ([x[0] for x in r], r)
    return cache[tk]


def run(path, p, act, drop, k, fast, minprofit, hold=3.0, dropk=0.0, fastk=0.0):
    """Return the exit bid (or None if held to settlement)."""
    peak = p
    below = None
    last, since = None, 0.0  # a repeated quote at the same bid does not restart the hold clock
    for t, b in path:
        if b != last:
            if last is not None and t - since >= hold:
                peak = max(peak, last)
            last, since = b, t
        elif t - since >= hold:
            peak = max(peak, b)
        armed = peak >= p * (1 + act) - 1e-9 and peak > p
        if not armed:
            below = None
            continue
        d = max(drop, k * (peak - p), dropk * peak)
        fast_d = max(fast, fastk * peak)
        if b < p * (1 + minprofit) + fee(b):
            below = None  # under the minimum profit: hold
            continue
        if b <= peak - fast_d + 1e-9:
            return b
        if b <= peak - d + 1e-9:
            below = t if below is None else below
            if t - below >= 10.0:
                return b
        else:
            below = None
    return None


def roi(p, exit_bid, won):
    cost = p + fee(p)
    ret = (exit_bid - SLIP - fee(exit_bid)) if exit_bid is not None else (1.0 if won else 0.0)
    return ret / cost - 1


variants = {
    "now: act10 drop4c fast12c": (0.10, 0.04, 0.0, 0.12, 0.0),
    "act10 drop proportional 0.25": (0.10, 0.04, 0.25, 0.12, 0.0),
    "act10 drop proportional 0.40": (0.10, 0.04, 0.40, 0.12, 0.0),
    "act20 drop4c fast12c": (0.20, 0.04, 0.0, 0.12, 0.0),
    "act30 drop4c fast12c": (0.30, 0.04, 0.0, 0.12, 0.0),
    "act20 proportional 0.30": (0.20, 0.04, 0.30, 0.12, 0.0),
    "act10 min profit 10%": (0.10, 0.04, 0.0, 0.12, 0.10),
    "act10 min profit 25%": (0.10, 0.04, 0.0, 0.12, 0.25),
    "act20 prop 0.30 min 15%": (0.20, 0.04, 0.30, 0.12, 0.15),
    "hold to settlement": None,
    "hold 1s, drop 35% of peak, fast 50%": (0.10, 0.0, 0.0, 0.0, 0.0, 0.9, 0.35, 0.50),
    "hold 1s, drop 25% of peak, fast 40%": (0.10, 0.0, 0.0, 0.0, 0.0, 0.9, 0.25, 0.40),
    "hold 3s, drop 35% of peak, fast 50%": (0.10, 0.0, 0.0, 0.0, 0.0, 3.0, 0.35, 0.50),
}
res = {k: [] for k in variants}
for tk, close, result in wins:
    ts, rows = quotes(tk)
    if not ts or ts[0] > close - 900 + 4 * 60:
        continue
    for m in MINUTES:
        t0 = close - (15 - m) * 60
        i = bisect.bisect_right(ts, t0) - 1
        if i < 0:
            continue
        for side in ("yes", "no"):
            p = rows[i][2] if side == "yes" else 1 - rows[i][1]
            if not LO <= p <= HI:
                continue
            path = [(x[0], x[1] if side == "yes" else 1 - x[2]) for x in rows[i + 1:] if x[0] < close - 20]
            won = result == side
            for name, v in variants.items():
                ex = None if v is None else run(path, p, *v)
                res[name].append(roi(p, ex, won))
n = len(res["hold to settlement"])
print(f"band {LO:.2f}-{HI:.2f}, {n} entries (every side/minute combination, so overlapping): mean ROI after fees\n")
for name, r in res.items():
    se = st.pstdev(r) / max(1, len(r)) ** 0.5
    print(f"{name:34} {st.mean(r):+7.1%}  (se {se:.1%})  wins {sum(x > 0 for x in r) / len(r):.0%}")
