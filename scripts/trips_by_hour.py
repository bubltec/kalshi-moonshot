"""Finished bot trades by the ET hour they closed in: silver against everything else. Read-only.
    scripts/cloud.sh run scripts/trips_by_hour.py [NAME=SILVER] [DAYS=7]
"""
import os
import re
import time
from collections import defaultdict
from zoneinfo import ZoneInfo
from datetime import datetime

from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.trips import build_trips

s = config.reload()
store = Store(s.db_path, s.exclude_versions)
NAME, DAYS = os.environ.get("NAME", "SILVER"), float(os.environ.get("DAYS", "7"))
since = time.time() - DAYS * 86400
et = ZoneInfo("America/New_York")
g = defaultdict(lambda: [[0, 0.0], [0, 0.0]])
for t in build_trips(store):
    if not (t.done and t.reliable and t.source == "bot" and t.series.endswith("15M") and t.closed_ts >= since):
        continue
    h = datetime.fromtimestamp(t.closed_ts, et).hour
    i = 0 if re.match(r"^KX([A-Z]+)15M$", t.series).group(1) == NAME else 1
    g[h][i][0] += 1
    g[h][i][1] += t.pnl
print(f"{'ET hr':>5} | {NAME:>7} n  P&L | others n  P&L")
for h in sorted(g):
    a, b = g[h]
    print(f"{h:>5} | {a[0]:>9} {a[1]:>+7.2f} | {b[0]:>8} {b[1]:>+7.2f}")
