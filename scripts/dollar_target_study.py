"""Can a 15-minute market pay a fixed dollar target per window, and at which minute is it best to enter? Replays ticks; places nothing.

Each window is entered once at each minute, on either side whose ask is in the band, with `N` contracts. The position is sold the first time
its bid is high enough that the sale nets `TARGET` dollars after both fees; otherwise it is held to settlement. Reported per minute: how
often the target was hit, the average dollars per entry, and the worst case. A minute whose average is not clearly over zero is no minute
to bet in, whatever the hit rate looks like.
    python scripts/dollar_target_study.py data/moonshot.db data/tape.db SERIES[,SERIES] [N=5] [TARGET=1.0] [low=0.25] [high=0.75] [STOP=0 (dollars of loss that sells; 0 = hold to settlement)]
"""
import bisect
import sqlite3
import statistics as st
import sys
from collections import defaultdict

from kalshi_moonshot.strategy import fee_per_contract as fee

db = sqlite3.connect(sys.argv[1])
tape = sqlite3.connect(sys.argv[2])
SERIES = sys.argv[3].split(",")
N = float(sys.argv[4]) if len(sys.argv) > 4 else 5.0
TARGET = float(sys.argv[5]) if len(sys.argv) > 5 else 1.0
LO = float(sys.argv[6]) if len(sys.argv) > 6 else 0.25
HI = float(sys.argv[7]) if len(sys.argv) > 7 else 0.75
STOP = float(sys.argv[8]) if len(sys.argv) > 8 else 0.0
q = ",".join("?" * len(SERIES))
wins = db.execute(f"select ticker, close_ts, result from windows where result in ('yes','no') and series in ({q}) order by close_ts", SERIES).fetchall()
cache = {}


def quotes(tk):
    if tk not in cache:
        r = tape.execute("select ts,bid,ask from quotes where ticker=? and bid is not null and ask is not null order by ts", (tk,)).fetchall()
        cache[tk] = ([x[0] for x in r], r)
    return cache[tk]


per = defaultdict(list)  # minute -> dollars
for tk, close, result in wins:
    ts, rows = quotes(tk)
    if not ts or ts[0] > close - 900 + 60:
        continue
    for m in range(1, 14):
        i = bisect.bisect_right(ts, close - (15 - m) * 60) - 1
        if i < 0 or close - (15 - m) * 60 - ts[i] > 20:
            continue
        for side in ("yes", "no"):
            p = rows[i][2] if side == "yes" else 1 - rows[i][1]
            if not LO <= p <= HI:
                continue
            cost = N * (p + fee(p))
            got = None
            for x in rows[i + 1:]:
                if x[0] >= close - 5:
                    break
                b = x[1] if side == "yes" else 1 - x[2]
                if N * (b - 0.02 - fee(b)) - cost >= TARGET:  # a 2c cushion under the bid, as the live sale uses
                    got = N * (b - 0.02 - fee(b)) - cost
                    break
                if STOP and N * (b - 0.02 - fee(b)) - cost <= -STOP:
                    got = N * (b - 0.02 - fee(b)) - cost
                    break
            pay = got if got is not None else N * (1.0 if result == side else 0.0) - cost
            per[m].append((pay, got is not None and got > 0))
print(f"{','.join(SERIES)}: {N:g} contracts, target ${TARGET:.2f} net, ask {LO:.2f}-{HI:.2f}, sold at the target else held to settlement\n")
print(f"{'minute':>6} {'n':>5} {'hit':>5} {'avg $':>7} {'se':>6} {'worst':>7}")
for m in sorted(per):
    v = [x[0] for x in per[m]]
    print(f"{m:6} {len(v):5} {sum(x[1] for x in per[m]) / len(v):5.0%} {st.mean(v):+7.2f} {st.pstdev(v) / len(v) ** 0.5:6.2f} {min(v):+7.2f}")
allv = [x[0] for m in per for x in per[m]]
print(f"\nall minutes: {len(allv)} entries, hit {sum(x[1] for m in per for x in per[m]) / len(allv):.0%}, average {st.mean(allv):+.2f} per entry")
