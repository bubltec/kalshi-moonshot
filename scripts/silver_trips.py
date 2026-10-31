"""One commodity's finished bot trades, split by how they ended, entry price and hour, next to the other commodities. Read-only.
    scripts/cloud.sh run scripts/silver_trips.py [NAME=SILVER] [DAYS=7]
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
NAME, DAYS = os.environ.get("NAME", "SILVER"), float(os.environ.get("DAYS", "7"))
since = time.time() - DAYS * 86400
allt = [t for t in build_trips(store) if t.done and t.reliable and t.source == "bot" and t.series.endswith("15M") and t.closed_ts >= since]
nm = lambda t: re.match(r"^KX([A-Z]+)15M$", t.series).group(1)  # noqa: E731
mine, rest = [t for t in allt if nm(t) == NAME], [t for t in allt if nm(t) != NAME]


def line(label, ts):
    if ts:
        print(f"  {label:<22} n {len(ts):>3}  won {sum(1 for t in ts if t.pnl > 0):>3}  P&L {sum(t.pnl for t in ts):>+7.2f}  avg entry {sum(t.entry_price for t in ts) / len(ts):.3f}")


for label, ts in ((NAME, mine), ("all others", rest)):
    print(f"== {label}")
    line("all", ts)
    line("held to settlement", [t for t in ts if t.cashed_out < 1e-9])
    line("cashed out", [t for t in ts if t.cashed_out >= 1e-9])
    for lo, hi in ((0.0, 0.82), (0.82, 0.86), (0.86, 1.0)):
        line(f"entry {lo:.2f}-{hi:.2f}", [t for t in ts if lo <= t.entry_price < hi])
    print()
print(f"== {NAME} trades, newest last: close ET, side, entry, exit, result, pnl")
for t in sorted(mine, key=lambda t: t.opened_ts):
    print(f"  {time.strftime('%m-%d %H:%M', time.localtime(t.closed_ts - 4 * 3600 if False else t.closed_ts))}Z {t.ticker[-10:]} {t.side:<3} in {t.entry_price:.2f} out {t.exit_price:.2f} {t.exit_type:<10} result {t.result} {t.pnl:>+6.2f}")
