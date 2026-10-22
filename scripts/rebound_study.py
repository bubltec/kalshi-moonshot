"""Does the past hour tell you which cheap (20-40c) side of a 15-minute market to buy? Replays recorded ticks; places nothing.

The window's strike is the price at its open, so the strikes of the four windows before it give the last hour's move. A cheap side
is a "rebound" when it bets against that move (YES after an hour of falling, NO after an hour of rising) and "with the trend" when
it bets with it. Entries are at the ask at minutes 4-12 of the window, held to settlement; ROI is after fees. The move is scaled by
the series' own typical 15-minute move, so gold and dogecoin count alike.
    python scripts/rebound_study.py data/moonshot.db data/tape.db [low high]
"""
import bisect
import sqlite3
import statistics as st
import sys
from collections import defaultdict

from kalshi_moonshot.strategy import fee_per_contract as fee

db = sqlite3.connect(sys.argv[1])
tape = sqlite3.connect(sys.argv[2])
LO, HI = (float(sys.argv[3]), float(sys.argv[4])) if len(sys.argv) > 4 else (0.20, 0.40)
rows = db.execute("select ticker, series, close_ts, strike, result from windows where result in ('yes','no') and strike is not null and series like '%15M' order by series, close_ts").fetchall()
by_series = defaultdict(list)
for r in rows:
    by_series[r[1]].append(r)
cache = {}


def quotes(tk):
    if tk not in cache:
        q = tape.execute("select ts,bid,ask from quotes where ticker=? and bid is not null and ask is not null order by ts", (tk,)).fetchall()
        cache[tk] = ([x[0] for x in q], q)
    return cache[tk]


out = defaultdict(list)  # bucket -> [roi]
for series, ws in by_series.items():
    step = [abs(b[3] / a[3] - 1) for a, b in zip(ws, ws[1:]) if a[3] and b[2] - a[2] == 900]
    if len(step) < 50:
        continue
    typical = st.median(step) or 1e-9
    idx = {w[2]: w for w in ws}
    for tk, _s, close, strike, result in ws:
        past = idx.get(close - 900 * 4)
        if past is None or not past[3]:
            continue
        move = (strike / past[3] - 1) / typical  # the last hour's move, in typical-15-minute-move units
        ts, q = quotes(tk)
        if not ts or ts[0] > close - 900 + 4 * 60:
            continue
        for m in (4, 6, 8, 10, 12):
            i = bisect.bisect_right(ts, close - (15 - m) * 60) - 1
            if i < 0:
                continue
            for side in ("yes", "no"):
                p = q[i][2] if side == "yes" else 1 - q[i][1]
                if not LO <= p <= HI:
                    continue
                roi = ((1.0 if result == side else 0.0) / (p + fee(p))) - 1
                rebound = (side == "yes" and move < 0) or (side == "no" and move > 0)
                size = abs(move)
                band = "tiny" if size < 1 else "1-2" if size < 2 else "2-4" if size < 4 else "4+"
                out[("rebound" if rebound else "with trend", band)].append(roi)
                out[("all", "all")].append(roi)
print(f"cheap side {LO:.2f}-{HI:.2f}, held to settlement, ROI after fees (hour move in typical-15-min-move units)\n")
print(f"{'side':12} {'hour move':10} {'n':>6} {'mean ROI':>9} {'se':>6} {'win%':>5}")
for key in sorted(out):
    r = out[key]
    se = st.pstdev(r) / len(r) ** 0.5
    print(f"{key[0]:12} {key[1]:10} {len(r):6} {st.mean(r):+9.1%} {se:6.1%} {sum(x > 0 for x in r) / len(r):5.0%}")
