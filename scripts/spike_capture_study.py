"""Of the cheap tickets that spiked to 3x or more, how much of the spike does each exit keep? Replays ticks; places nothing.

Entries are the 2-12 cent side at minutes 10-14 of a 15-minute window (the lottery-ticket case). Only entries whose bid later reached
3x the price paid are kept. For each exit, `hold` is how long a bid must stand before it counts as the peak, and the position is sold when
the bid falls `drop` (a share of the peak) under that peak. Reported: the mean multiple of the price paid that the exit sold at, beside the
best bid seen (the ceiling no exit can reach) and what holding to settlement paid.
    python scripts/spike_capture_study.py data/moonshot.db data/tape.db
"""
import bisect
import sqlite3
import statistics as st
import sys

db = sqlite3.connect(sys.argv[1])
tape = sqlite3.connect(sys.argv[2])
wins = db.execute("select ticker, close_ts, result from windows where result in ('yes','no') and series like '%15M' order by close_ts").fetchall()
cache = {}


def quotes(tk):
    if tk not in cache:
        r = tape.execute("select ts,bid,ask from quotes where ticker=? and bid is not null and ask is not null order by ts", (tk,)).fetchall()
        cache[tk] = ([x[0] for x in r], r)
    return cache[tk]


def exit_at(path, p, hold, drop):
    peak, last, since = p, None, 0.0  # a repeated quote at the same bid does not restart the hold clock
    for t, b in path:
        if b != last:
            if last is not None and t - since >= hold and last > peak:
                peak = last
            last, since = b, t
        elif t - since >= hold and b > peak:
            peak = b
        if peak >= p * 1.10 and peak > p and b <= peak * (1 - drop):
            return b
    return None


variants = [(3.0, 0.50), (3.0, 0.30), (1.0, 0.50), (1.0, 0.30), (0.0, 0.50), (0.0, 0.30)]
ceil, settle = [], []
res = {v: [] for v in variants}
n_all = 0
for tk, close, result in wins:
    ts, rows = quotes(tk)
    if not ts or ts[0] > close - 900 + 10 * 60:
        continue
    for m in (10, 11, 12, 13, 14):
        i = bisect.bisect_right(ts, close - (15 - m) * 60) - 1
        if i < 0:
            continue
        for side in ("yes", "no"):
            p = rows[i][2] if side == "yes" else 1 - rows[i][1]
            if not 0.02 <= p <= 0.12:
                continue
            path = [(x[0], x[1] if side == "yes" else 1 - x[2]) for x in rows[i + 1:] if x[0] < close - 5]
            if not path:
                continue
            n_all += 1
            top = max(b for _, b in path)
            if top < 3 * p:
                continue
            ceil.append(top / p)
            settle.append((1.0 if result == side else 0.0) / p)
            for v in variants:
                ex = exit_at(path, p, *v)
                res[v].append((ex if ex is not None else (1.0 if result == side else 0.0)) / p)
print(f"{n_all} cheap entries, {len(ceil)} whose bid reached 3x ({len(ceil) / n_all:.1%})\n")
print(f"best bid seen (ceiling)       mean {st.mean(ceil):5.1f}x")
print(f"hold to settlement            mean {st.mean(settle):5.1f}x")
for (hold, drop), r in res.items():
    print(f"hold {hold:3.0f}s, sell {drop:.0%} off peak  mean {st.mean(r):5.1f}x   ({st.mean(r) / st.mean(ceil):.0%} of ceiling)")
