"""IS A FAST DROP AN EXIT: a held favorite (bid 0.90+) whose bid falls D cents within W seconds in the last 120 s of a 15-minute market. The burst exit waits for volume AND a bid still at 0.90; this asks what the fall ALONE says.

For every finished 15-minute window on the tape (all series: crypto, commodities, currencies), each side whose bid was 0.90+ W seconds earlier, in the last 120 s down to 10 s: the first second its bid is D under that. One event per window and side.
Then, per event: how often the side still WON at settlement (the fall was noise), and what selling is worth against holding. Selling is priced two ways: at the bid at the event, and at the bid ONE SECOND LATER (an order takes a moment, and in a crash the bid keeps falling),
with the sale fee paid. Holding pays 1 if the side wins and 0 if it loses (no fee). "Selling is better by" is the average sale proceeds minus the average hold payout, per contract, with a standard error.
Read-only. Places nothing.
    docker compose exec -T bot python - < scripts/fast_drop_study.py
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
WINDOWS = main.db.execute("SELECT ticker, series, result, close_ts FROM windows WHERE series LIKE '%15M' AND result IN ('yes','no') ORDER BY close_ts").fetchall()
FAVORITE = 0.90
DROPS = (0.05, 0.08, 0.10, 0.15, 0.20)
SPEEDS = (1, 2, 3)


def side_bids(q, close):
    """Per second, the YES side's bid and the NO side's bid (1 - the YES ask), from the last quote at or before the end of that second."""
    ts = [float(r[0]) for r in q]
    out = {}
    for sec in range(int(close) - 135, int(close) + 1):
        i = bisect.bisect_right(ts, sec + 0.999) - 1
        if i < 0 or sec + 0.999 - ts[i] > 15:
            continue
        out[sec] = {"yes": float(q[i][1]), "no": 1.0 - float(q[i][2])}
    return out


events = {(d, w): [] for d in DROPS for w in SPEEDS}
n_windows = 0
for m in WINDOWS:
    close = float(m["close_ts"])
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts>? AND ts<=? AND bid > 0 AND ask > 0 ORDER BY ts", (m["ticker"], close - 140, close)).fetchall()
    if len(q) < 40:
        continue
    n_windows += 1
    bids = side_bids(q, close)
    for side in ("yes", "no"):
        for d in DROPS:
            for w in SPEEDS:
                for sec in range(int(close) - 120, int(close) - 9):
                    now, before, after = bids.get(sec), bids.get(sec - w), bids.get(sec + 1)
                    if not now or not before or not after:
                        continue
                    if before[side] >= FAVORITE and before[side] - now[side] >= d - 1e-9:
                        won = 1.0 if m["result"] == side else 0.0
                        events[(d, w)].append((m["ticker"], m["series"], side, before[side], now[side], after[side], won))
                        break


def stat(rows, price_idx):
    n = len(rows)
    if not n:
        return None
    sale = [max(r[price_idx], 0.0) - (fee(r[price_idx]) if r[price_idx] > 0 else 0.0) for r in rows]
    hold = [r[6] for r in rows]
    diff = [a - b for a, b in zip(sale, hold, strict=True)]
    se = statistics.pstdev(diff) / math.sqrt(n) if n > 1 else float("nan")
    return n, statistics.mean(hold), statistics.mean(r[price_idx] for r in rows), statistics.mean(diff), se


print(f"{n_windows} finished 15-minute windows replayed. An event: a side at {FAVORITE:.2f}+ whose bid falls D within W seconds, in the last 120 s.\n")
print(f"{'drop D':>7} {'in W s':>6} {'events':>7} {'still won':>10} | {'sale at the event':>40} | {'sale 1 s later (an order takes a moment)':>46}")
print(f"{'':>7} {'':>6} {'':>7} {'':>10} | {'avg sale':>9} {'selling better by':>20} {'+-':>6}   | {'avg sale':>9} {'selling better by':>20} {'+-':>6}")
for d in DROPS:
    for w in SPEEDS:
        ev = events[(d, w)]
        a, b = stat(ev, 4), stat(ev, 5)
        if a is None:
            continue
        print(f"{d:>7.2f} {w:>6} {a[0]:>7} {100*a[1]:>9.1f}% | {a[2]:>9.3f} {a[3]:>+19.3f} {a[4]:>6.3f}   | {b[2]:>9.3f} {b[3]:>+19.3f} {b[4]:>6.3f}")

print("\nBy kind of market, for a 10c fall within 2 s (sale 1 s later):")
ev = events[(0.10, 2)]
for label, pick in (("crypto", lambda r: r[1][2:5] in ("BTC", "ETH", "SOL", "XRP", "DOG")), ("not crypto", lambda r: r[1][2:5] not in ("BTC", "ETH", "SOL", "XRP", "DOG"))):
    b = stat([r for r in ev if pick(r)], 5)
    if b:
        print(f"  {label:11} events {b[0]:>4}  still won {100*b[1]:.1f}%  avg sale {b[2]:.3f}  selling better by {b[3]:+.3f} +-{b[4]:.3f}")
