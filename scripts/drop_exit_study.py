"""SHOULD A FAST FALL IN THE HELD SIDE'S BID SELL IT, WHATEVER THE TAKER FLOW: the burst exit keys on dollars bought against the holder, and needs the bid at a floor. On KXCOPPER15M-26OCT301000 (2026-10-30) a NO bought at 0.88 fell 0.85 -> 0.74 -> 0.55 in four seconds
with almost no taker flow (the book thinned), and the position settled at 0. This replays a price rule over every finished 15-minute window on the tape: in the last 120 s (not the last 10), sell a held side whose bid is `drop` under its highest bid of the last `look` seconds.

A held side: its bid was 0.76+ at some point in the 60 s before (the lane buys favorites at 0.78-0.92), as in burst_floor_study. One event per window and side, at the first second the rule is met. Selling is priced at the bid ONE SECOND LATER, sale fee paid. Holding pays 1 if the side
wins, 0 if it loses. The tape keeps one quote a second, so a fall inside a second is invisible here.
Reported per (drop, look): events, how often the side still won (the fall was noise), the average sale price, and how much better selling is than holding per contract with a standard error. Also how many events a window set has, so false sales can be weighed against the saves.
Read-only. Places nothing.
    docker compose exec -T bot python - < scripts/drop_exit_study.py        (the cloud: scripts/cloud.sh run scripts/drop_exit_study.py)
"""
import bisect
import math
import statistics

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
COMMODITY = ("GOLD", "SILVER", "WTI", "BRENT", "NATGAS", "COPPER", "PLATINUM", "PALLADIUM")
WINDOWS = [w for w in main.db.execute("SELECT ticker, series, result, close_ts FROM windows WHERE series LIKE '%15M' AND result IN ('yes','no') ORDER BY close_ts").fetchall() if w["series"][2:-3] in COMMODITY]
DROPS = (0.10, 0.15, 0.20, 0.25, 0.30)
LOOKS = (3, 5, 10)
WAS_FAVORITE = 0.76
LAST, FIRST = 120, 10  # the exit window: seconds before the close

events = {(d, lk): [] for d in DROPS for lk in LOOKS}
n_windows = 0
for m in WINDOWS:
    close = float(m["close_ts"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? AND bid > 0 AND ask > 0 ORDER BY ts", (m["ticker"], close - 200, close)).fetchall()
    if len(q) < 40:
        continue
    n_windows += 1
    ts_list = [float(r[0]) for r in q]
    bids = {}
    for sec in range(int(close) - 200, int(close) + 1):
        i = bisect.bisect_right(ts_list, sec + 0.999) - 1
        if i >= 0 and sec + 0.999 - ts_list[i] <= 15:
            bids[sec] = {"yes": float(q[i][1]), "no": 1.0 - float(q[i][2])}
    for side in ("yes", "no"):
        fired = dict.fromkeys(events, False)
        for sec in range(int(close) - LAST, int(close) - FIRST):
            now, after = bids.get(sec), bids.get(sec + 1)
            if not now or not after:
                continue
            if max((bids[x][side] for x in range(sec - 60, sec) if x in bids), default=0.0) < WAS_FAVORITE:
                continue
            for key in events:
                d, lk = key
                if fired[key]:
                    continue
                high = max((bids[x][side] for x in range(sec - lk, sec + 1) if x in bids), default=0.0)
                if high - now[side] >= d - 1e-9:
                    fired[key] = True
                    events[key].append((m["series"], side, now[side], after[side], 1.0 if m["result"] == side else 0.0))


def stat(rows):
    n = len(rows)
    if not n:
        return None
    sale = [max(r[3], 0.0) - (fee(r[3]) if r[3] > 0 else 0.0) for r in rows]
    diff = [a - r[4] for a, r in zip(sale, rows, strict=True)]
    se = statistics.pstdev(diff) / math.sqrt(n) if n > 1 else float("nan")
    return n, statistics.mean(r[4] for r in rows), statistics.mean(r[3] for r in rows), statistics.mean(diff), se


print(f"{n_windows} finished commodity 15-minute windows replayed (last {LAST} s to {FIRST} s). Held side: 0.76+ in the 60 s before.\n")
print("selling better than holding, per contract [events] (+- standard error). Rows: how far the bid fell. Columns: over how many seconds.\n")
print(f"{'drop':>6} | " + " | ".join(f"within {lk:>2} s".rjust(24) for lk in LOOKS))
for d in DROPS:
    cells = []
    for lk in LOOKS:
        r = stat(events[(d, lk)])
        cells.append((f"{r[3]:+.3f} +-{r[4]:.3f} [{r[0]}]" if r else "-").rjust(24))
    print(f"{d:>6.2f} | " + " | ".join(cells))
print("\nStill won (the fall was noise) / average sale price at the same cells:")
for d in DROPS:
    cells = []
    for lk in LOOKS:
        r = stat(events[(d, lk)])
        cells.append((f"won {100 * r[1]:.0f}%  sold {r[2]:.2f}" if r else "-").rjust(24))
    print(f"{d:>6.2f} | " + " | ".join(cells))
print("\nWhere the fall left the bid (sale bid one second later) for drop 0.15 within 5 s: share of events by sale price")
rows = events[(0.15, 5)]
for lo, hi in ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)):
    sel = [r for r in rows if lo <= r[3] < hi]
    r = stat(sel)
    print(f"  sold {lo:.1f}-{min(hi, 1.0):.1f}: " + (f"{r[0]} events, won {100 * r[1]:.0f}%, selling better by {r[3]:+.3f}" if r else "-"))
