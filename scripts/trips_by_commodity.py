"""Realized results by commodity, from the exchange's own fills (trips.py), after every fee. Read-only.

Every finished bot trade of the last DAYS days on a 15-minute series, grouped by what it trades, with an ALL row. Cash-outs (sold before the close) and settlements are both counted, so exits that fired show up here.
    scripts/cloud.sh run scripts/trips_by_commodity.py [DAYS=7]
"""
import os
import re
import time
from collections import defaultdict

from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.trips import build_trips

s = config.reload()
store = Store(s.db_path, s.exclude_versions)
DAYS = float(os.environ.get("DAYS", "7"))
since = time.time() - DAYS * 86400
trips = [t for t in build_trips(store) if t.done and t.reliable and t.source == "bot" and t.series.endswith("15M") and t.closed_ts >= since]
g = defaultdict(list)
for t in trips:
    m = re.match(r"^KX([A-Z]+)15M$", t.series)
    g[m.group(1) if m else t.series].append(t)
print(f"{len(trips)} finished bot trades, last {DAYS:g} days, after fees (exchange fills)\n")
print(f"{'market':<10} {'n':>4} {'won':>4} {'cashed out':>10} {'staked':>8} {'P&L':>8} {'ROI':>7} {'worst':>7}")
rows = sorted(g.items(), key=lambda kv: sum(t.pnl for t in kv[1]))
for name, ts in rows + [("ALL", trips)]:
    staked = sum(t.cost for t in ts)
    pnl = sum(t.pnl for t in ts)
    print(f"{name:<10} {len(ts):>4} {sum(1 for t in ts if t.pnl > 0):>4} {sum(1 for t in ts if t.cashed_out > 1e-9):>10} {staked:>8.2f} {pnl:>+8.2f} {pnl / staked:>+7.1%} {min(t.pnl for t in ts):>+7.2f}")
