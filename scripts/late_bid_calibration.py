"""Is the market's own bid ever too generous in the last seconds? Calibration of the held bid against how the market ended, on EVERY settled commodity 15-minute market on the tape (not only our bets). Read-only.

For each settled market and each checkpoint L seconds before the close, the YES bid (the price a YES holder could sell at) and the NO bid (1 - YES ask) are taken; each side's bucket of bid is compared with how often that side then WON. A well-priced
bid wins as often as it costs. "edge" = win rate minus mean bid, in cents; a negative edge in a bucket means holders there were better off SELLING at the bid than holding (before the sale's taker fee, shown as `fee`). Rows with fewer than 15 sides are dropped.
The buckets are also split by whether the bid was falling or rising over the 10 s before the checkpoint.

    scripts/cloud.sh run scripts/late_bid_calibration.py [DAYS=7]
"""
import os
import re
import time

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract

s = config.reload()
tape = db.connect(s.tape_path, "tape", shared=True)
main = Store(s.db_path, s.exclude_versions)
DAYS = float(os.environ.get("DAYS", "7"))
WANT = {"GOLD", "SILVER", "PLATINUM", "PALLADIUM", "COPPER", "WTI", "BRENT", "NATGAS"}
CHECK = (60, 30, 15, 8, 4)
BUCKETS = ((0.05, 0.20), (0.20, 0.35), (0.35, 0.50), (0.50, 0.65), (0.65, 0.80), (0.80, 0.95))

since = time.time() - DAYS * 86400
rows = []  # (L, bid, won, trend)
for w in main.db.execute("SELECT ticker, series, close_ts, result FROM windows WHERE result IN ('yes','no') AND close_ts >= ? ORDER BY close_ts", (since,)).fetchall():
    m = re.match(r"^KX([A-Z]+)15M$", w["series"] or "")
    if not m or m.group(1) not in WANT:
        continue
    close = float(w["close_ts"])
    q = [(float(t), float(bid), float(ask)) for t, bid, ask in tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts", (w["ticker"], close - 80, close)).fetchall()]
    if len(q) < 10:
        continue

    def at(t):
        r = next((x for x in reversed(q) if x[0] <= t and t - x[0] <= 6), None)
        return r

    for L in CHECK:
        now, before = at(close - L), at(close - L - 10)
        if not now or not before:
            continue
        for side in ("yes", "no"):
            bid = now[1] if side == "yes" else 1.0 - now[2]
            bid0 = before[1] if side == "yes" else 1.0 - before[2]
            if bid <= 0.0:
                continue
            rows.append((L, bid, w["result"] == side, "falling" if bid < bid0 - 0.03 else "rising" if bid > bid0 + 0.03 else "flat"))
print(f"{len(rows)} (checkpoint, side) rows from {DAYS:g} days of settled commodity markets\n")
for split in ("all", "falling", "flat", "rising"):
    print(f"== bid trend over the 10 s before: {split}")
    print(f"{'left':>5} {'bid':>11} | {'n':>5} {'win%':>6} {'mean bid':>8} {'edge':>7} {'fee':>5}")
    for L in CHECK:
        for lo, hi in BUCKETS:
            g = [r for r in rows if r[0] == L and lo <= r[1] < hi and (split == "all" or r[3] == split)]
            if len(g) < 15:
                continue
            win = sum(1 for r in g if r[2]) / len(g)
            mb = sum(r[1] for r in g) / len(g)
            print(f"{L:>4}s {lo:.2f}-{hi:.2f} | {len(g):>5} {win:>6.1%} {mb:>8.3f} {(win - mb) * 100:>+6.1f}c {fee_per_contract(mb) * 100:>4.1f}c")
    print()
print("DONE")
