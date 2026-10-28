"""SHOULD THE BURST EXIT HAVE A FLOOR: the exit sells a held side when dollars bought AGAINST it in 3 s reach a bar, in the last 120 s, but only while the side's bid is still at a floor (burst_favorite, 0.90). This replays the exit with other floors over every finished
15-minute window on the tape, to see what the floor gives up or keeps.

A held side: its bid was 0.76+ at some point in the 60 s before (the lane buys favorites at 0.76-0.92). Each second from 120 s to 10 s before the close: the dollars the OTHER side's takers bought in the last 3 s (YES takers pay the YES price, NO takers 1 - the YES price; the tape keeps
one VWAP a second, so these run a little high against the bot's own ledger), against the bar for that kind of market (BTC 800, other crypto 400, commodities 400, currencies 50: the lanes' burst_dollars). The exit fires at the first second the bar is reached AND the bid is at or over the floor.
One event per window and side. Selling is priced at the bid ONE SECOND LATER (an order takes a moment), sale fee paid. Holding pays 1 if the side wins, 0 if it loses.
Reported per floor: events, how often the side still won (the burst was noise), the average sale price, and how much better selling is than holding, per contract, with a standard error.
Read-only. Places nothing.
    docker compose exec -T bot python - < scripts/burst_floor_study.py
"""
import bisect
import math
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
WINDOWS = main.db.execute("SELECT ticker, series, result, close_ts FROM windows WHERE series LIKE '%15M' AND result IN ('yes','no') ORDER BY close_ts").fetchall()
CRYPTO = ("BTC", "ETH", "SOL", "XRP", "DOGE")
COMMODITY = ("GOLD", "SILVER", "WTI", "BRENT", "NATGAS", "COPPER", "PLATINUM", "PALLADIUM")
FLOORS = (0.90, 0.85, 0.80, 0.70, 0.60, 0.50, 0.0)
WAS_FAVORITE = 0.76


def kind(series):
    name = series[2:-3]
    if name in CRYPTO:
        return "crypto"
    return "commodity" if name in COMMODITY else "currency"


def bar(series):
    name = series[2:-3]
    if name == "BTC":
        return 800.0
    return 400.0 if kind(series) != "currency" else 50.0


events = {f: [] for f in FLOORS}
n_windows = 0
for m in WINDOWS:
    close = float(m["close_ts"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? AND bid > 0 AND ask > 0 ORDER BY ts", (m["ticker"], close - 200, close)).fetchall()
    fl = tape.execute("SELECT ts, yes_vol, no_vol, vwap FROM flow_1s WHERE ticker=? AND ts>? AND ts<=?", (m["ticker"], int(close - 205), int(close))).fetchall()
    if len(q) < 40 or len(fl) < 20:
        continue
    n_windows += 1
    yd, nd = defaultdict(float), defaultdict(float)
    for ts, yv, nv, vw in fl:
        if vw is not None:
            yd[int(ts)] += float(yv) * float(vw)
            nd[int(ts)] += float(nv) * (1.0 - float(vw))
    ts_list = [float(r[0]) for r in q]
    bids = {}
    for sec in range(int(close) - 200, int(close) + 1):
        i = bisect.bisect_right(ts_list, sec + 0.999) - 1
        if i >= 0 and sec + 0.999 - ts_list[i] <= 15:
            bids[sec] = {"yes": float(q[i][1]), "no": 1.0 - float(q[i][2])}
    the_bar = bar(m["series"])
    for side, against in (("yes", nd), ("no", yd)):
        fired = dict.fromkeys(FLOORS, False)
        for sec in range(int(close) - 120, int(close) - 9):
            now, after = bids.get(sec), bids.get(sec + 1)
            if not now or not after:
                continue
            if max((bids[x][side] for x in range(sec - 60, sec) if x in bids), default=0.0) < WAS_FAVORITE:
                continue
            d3 = against[sec] + against[sec - 1] + against[sec - 2]
            if d3 < the_bar:
                continue
            for f in FLOORS:
                if not fired[f] and now[side] >= f:
                    fired[f] = True
                    events[f].append((m["series"], side, now[side], after[side], 1.0 if m["result"] == side else 0.0))


def stat(rows):
    n = len(rows)
    if not n:
        return None
    sale = [max(r[3], 0.0) - (fee(r[3]) if r[3] > 0 else 0.0) for r in rows]
    diff = [a - r[4] for a, r in zip(sale, rows, strict=True)]
    se = statistics.pstdev(diff) / math.sqrt(n) if n > 1 else float("nan")
    return n, statistics.mean(r[4] for r in rows), statistics.mean(r[3] for r in rows), statistics.mean(diff), se


print(f"{n_windows} finished 15-minute windows replayed. A held side was 0.76+ in the 60 s before; the exit fires once per window and side at the first second the bar is reached with the bid at or over the floor.\n")
print(f"{'floor':>6} | {'events':>6} {'still won':>9} {'avg sale':>9} {'selling better by':>18} {'+-':>6}")
for f in FLOORS:
    r = stat(events[f])
    if r:
        print(f"{f:>6.2f} | {r[0]:>6} {100*r[1]:>8.1f}% {r[2]:>9.3f} {r[3]:>+18.3f} {r[4]:>6.3f}")
for label in ("crypto", "commodity", "currency"):
    print(f"\n{label}:")
    for f in (0.90, 0.80, 0.70, 0.0):
        r = stat([e for e in events[f] if kind(e[0]) == label])
        if r:
            print(f"  floor {f:>4.2f}: events {r[0]:>4}  still won {100*r[1]:>5.1f}%  avg sale {r[2]:.3f}  selling better by {r[3]:+.3f} +-{r[4]:.3f}")
print("\nThe events the 0.90 floor leaves out, but a lower floor takes (floor 0.70 minus floor 0.90):")
extra = [e for e in events[0.70] if e not in events[0.90]]
r = stat(extra)
if r:
    print(f"  events {r[0]}  still won {100*r[1]:.1f}%  avg sale {r[2]:.3f}  selling better by {r[3]:+.3f} +-{r[4]:.3f}")
