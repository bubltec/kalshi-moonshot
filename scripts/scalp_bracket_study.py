"""Would a plain bracket make a steady 10% on the early scalp? Buy, rest a sell at +UP over the price paid, stop at DOWN under it.

Every early_scalp bet on record is replayed from its entry on the recorded quotes. The sell fills at its price the moment the side's bid
touches it (a resting sell, so no taker fee); the stop sells at the bid of the quote that crossed it (a taker sale, a few cents worse in
life). Nothing else sells. A bet that touches neither settles. Returns are per dollar staked, before fees. The tape keeps about one quote
per second, so a bid that touched a price between two quotes is not seen.

Read-only. Places nothing.
    scripts/cloud.sh run scripts/scalp_bracket_study.py [LANE=early_scalp] [UPS=0.10] [DOWNS=0.10,0.20,0.30,none]
"""
import math
import os
from datetime import datetime

from kalshi_moonshot import config, db
from kalshi_moonshot.journal import cluster_se
from kalshi_moonshot.store import Store

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)

LANE = os.environ.get("LANE", "early_scalp")
UPS = tuple(float(x) for x in os.environ.get("UPS", "0.10").split(","))
DOWNS = tuple(math.inf if x == "none" else float(x) for x in os.environ.get("DOWNS", "0.10,0.20,0.30,none").split(","))


def simulate(entry, side, won, path, up, down):
    """(roi per dollar, how it ended: 'up', 'down' or 'settle') for one bet."""
    target = entry * (1.0 + up)
    line = entry * (1.0 - down) if not math.isinf(down) else -1.0
    for ts, bid, ask in path:
        b = bid if side == "yes" else 1.0 - ask
        if b >= target - 1e-9:
            return up, "up"
        if b <= line + 1e-9:
            return b / entry - 1.0, "down"
    return ((1.0 / entry - 1.0) if won else -1.0), "settle"


rows = main.db.execute(
    "SELECT ts, ticker, side, ask, outcome, close_time FROM decisions "
    "WHERE action='bet' AND lane=? AND outcome IN ('yes','no') ORDER BY ts",
    (LANE,),
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
        bets.append((float(r["ask"]), r["side"] or "yes", r["outcome"] == (r["side"] or "yes"), path, str(r["close_time"])))

print(f"{len(bets)} {LANE} bets replayed, avg entry {sum(b[0] for b in bets) / len(bets):.2f}")
print(f"{'up':>5} {'down':>6} {'mean ROI':>9} {'SE':>6} {'hit up':>7} {'stopped':>8} {'settled':>8} {'avg stop':>9} {'avg settle':>11}", flush=True)
for up in UPS:
    for down in DOWNS:
        rois, pairs, ends = [], [], {"up": [], "down": [], "settle": []}
        for entry, side, won, path, slot in bets:
            roi, how = simulate(entry, side, won, path, up, down)
            rois.append(roi)
            pairs.append((slot, roi))
            ends[how].append(roi)
        mean = sum(rois) / len(rois)
        def avg(k):
            return f"{sum(ends[k]) / len(ends[k]):>+9.1%}" if ends[k] else f"{'-':>9}"
        name = "none" if math.isinf(down) else f"{down:.0%}"
        print(f"{up:>5.0%} {name:>6} {mean:>+9.1%} {cluster_se(pairs, mean):>6.1%} {len(ends['up']) / len(bets):>7.0%} "
              f"{len(ends['down']):>8} {len(ends['settle']):>8} {avg('down')} {avg('settle'):>11}")
