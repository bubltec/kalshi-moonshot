"""How many seconds before the close should a losing favorite be sold? Replayed from the tape (strategy.cash_if_losing).

Every commodity 15-minute bet on record is replayed from its entry on the recorded quotes with only the last-seconds cash-out acting: with
N seconds or fewer left, the first quote whose bid is under the price paid sells at that bid, provided the last 3 seconds of bids were a
hover (high minus low under 8c; HOVER=0 drops that condition). A bid at or above the price paid is left to settle, and so is a bet that
never meets the rule. Nothing else sells: no stop, no burst exit. N=0 is the plain hold. Returns are per dollar staked, before fees. The
sale is at the quote's bid; a real sale lands a cent or two under it. The tape keeps about one quote per second.

Read-only. Places nothing.
    scripts/cloud.sh run scripts/last_seconds_study.py [LANES=commodity] [MODES=trade,observe] [HOVER=1] [SECONDS=0,5,10,20,30]
"""
import os
from datetime import datetime

from kalshi_moonshot import config, db
from kalshi_moonshot.journal import cluster_se
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

LANES = tuple(os.environ.get("LANES", "commodity").split(","))
MODES = tuple(os.environ.get("MODES", "trade,observe").split(","))
HOVER = os.environ.get("HOVER", "1") != "0"
SECONDS = tuple(float(x) for x in os.environ.get("SECONDS", "0,5,10,20,30").split(","))


def simulate(entry, side, won, path, close_ts, within):
    """(roi per dollar, sold early?) for one bet."""
    if within > 0:
        bids = [(ts, (bid if side == "yes" else 1.0 - ask)) for ts, bid, ask in path]
        for i, (ts, b) in enumerate(bids):
            left = close_ts - ts
            if left > within or left < 0 or b <= 0 or b >= entry:
                continue
            recent = [x for t, x in bids[: i + 1] if ts - 3.0 <= t <= ts and x > 0]
            if HOVER and (len(recent) < 2 or max(recent) - min(recent) >= 0.08):
                continue
            return b / entry - 1.0, True
    return ((1.0 / entry - 1.0) if won else -1.0), False


rows = main.db.execute(
    f"""SELECT ts, ticker, side, ask, outcome, close_time FROM decisions
        WHERE action='bet' AND lane IN ({','.join('?' * len(LANES))}) AND mode IN ({','.join('?' * len(MODES))})
          AND outcome IN ('yes','no') AND ticker LIKE '%15M%' ORDER BY ts""",
    LANES + MODES,
).fetchall()

bets = []
cache: dict[str, list] = {}
for r in rows:
    if r["ticker"] not in cache:
        cache[r["ticker"]] = tape.execute(
            "SELECT ts, bid, ask FROM quotes WHERE ticker=? AND bid > 0 AND ask > 0 ORDER BY ts", (r["ticker"],)
        ).fetchall()
    try:
        close_ts = datetime.fromisoformat(str(r["close_time"])).timestamp()
    except (TypeError, ValueError):
        continue
    path = [q for q in cache[r["ticker"]] if float(r["ts"]) <= q[0] <= close_ts]
    if path:
        bets.append((float(r["ask"]), r["side"] or "yes", r["outcome"] == (r["side"] or "yes"), path, close_ts, str(r["close_time"])))

print(f"{len(bets)} {'/'.join(LANES)} bets, last-seconds cash-out {'with' if HOVER else 'without'} the hover condition")
print(f"{'secs':>5} {'mean ROI':>9} {'SE':>6} {'sold':>5} {'sold winners':>12} {'sold losers':>11} {'avg sale':>9} {'saved $/bet':>11}", flush=True)
hold = None
for n in SECONDS:
    rois, pairs, sold_w, sold_l, sale = [], [], 0, 0, []
    for entry, side, won, path, close_ts, slot in bets:
        roi, early = simulate(entry, side, won, path, close_ts, n)
        rois.append(roi)
        pairs.append((slot, roi))
        if early:
            sale.append(roi)
            sold_w += 1 if won else 0
            sold_l += 0 if won else 1
    mean = sum(rois) / len(rois)
    if n == 0:
        hold = mean
    avg = f"{sum(sale) / len(sale):>+9.1%}" if sale else f"{'-':>9}"
    diff = f"{(mean - hold):>+11.1%}" if hold is not None and n else f"{'-':>11}"
    print(f"{n:>5.0f} {mean:>+9.1%} {cluster_se(pairs, mean):>6.1%} {len(sale):>5} {sold_w:>12} {sold_l:>11} {avg} {diff}")
