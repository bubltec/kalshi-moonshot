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
SHARES = (0.0, 0.5, 0.75, 0.9)  # the share of the last 3 s of dollars that was AGAINST the held side (0 = no condition). A pump of the held side itself is a low share
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


events = {(f, sh): [] for f in FLOORS for sh in SHARES}
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
    for side, against, withh in (("yes", nd, yd), ("no", yd, nd)):
        fired = dict.fromkeys(events, False)
        for sec in range(int(close) - 120, int(close) - 9):
            now, after = bids.get(sec), bids.get(sec + 1)
            if not now or not after:
                continue
            if max((bids[x][side] for x in range(sec - 60, sec) if x in bids), default=0.0) < WAS_FAVORITE:
                continue
            d3 = against[sec] + against[sec - 1] + against[sec - 2]
            if d3 < the_bar:
                continue
            w3 = withh[sec] + withh[sec - 1] + withh[sec - 2]
            share = d3 / (d3 + w3 + 1e-9)
            for key in events:
                f, sh = key
                if not fired[key] and now[side] >= f and share >= sh:
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


print(f"{n_windows} finished 15-minute windows replayed. A held side was 0.76+ in the 60 s before; the exit fires once per window and side at the first second the bar is reached with the bid at or over the floor.\n")
print("selling better than holding, per contract (events in brackets; +- is the standard error). Rows: the floor on the held side's bid. Columns: the share of the 3-second dollars that was AGAINST the held side.\n")
print(f"{'floor':>6} | " + " | ".join(f"share >= {sh:.2f}".rjust(22) for sh in SHARES))
for f in FLOORS:
    cells = []
    for sh in SHARES:
        r = stat(events[(f, sh)])
        cells.append((f"{r[3]:+.3f} +-{r[4]:.3f} [{r[0]}]" if r else "-").rjust(22))
    print(f"{f:>6.2f} | " + " | ".join(cells))
print("\nStill won (the burst was noise) at the same cells:")
for f in (0.90, 0.80, 0.0):
    print(f"{f:>6.2f} | " + " | ".join((f"{100*stat(events[(f, sh)])[1]:.1f}%" if events[(f, sh)] else "-").rjust(22) for sh in SHARES))
for label in ("crypto", "commodity", "currency"):
    print(f"\n{label} (selling better by, events), floor 0.90 and no floor, share 0 / 0.75:")
    for f in (0.90, 0.0):
        row = []
        for sh in (0.0, 0.75):
            r = stat([e for e in events[(f, sh)] if kind(e[0]) == label])
            row.append((f"{r[3]:+.3f} +-{r[4]:.3f} [{r[0]}]" if r else "-").rjust(24))
        print(f"  floor {f:>4.2f}: " + " | ".join(row))
