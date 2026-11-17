"""Print the account's finished round trips since 2026-11-09 as rows, rebuilt from the raw fills by trips.py (a trip: flat to a position and back to flat, or to settlement). Read-only; one pass over the fills table.
    scripts/cloud.sh run scripts/hand_trips_dump.py > data/tape_archive/trips.csv   (keep the lines that start with T,)
Read by scripts/hand_trips_study.py on the Mac."""
import datetime as dt
from zoneinfo import ZoneInfo

from kalshi_moonshot import config, trips
from kalshi_moonshot.store import Store

s = config.reload()
T0 = dt.datetime(2026, 11, 9, tzinfo=ZoneInfo("America/New_York")).timestamp()
store = Store(s.db_path, s.exclude_versions)
n = 0
for t in trips.build_trips(store):
    if not t.done or t.opened_ts < T0:
        continue
    n += 1
    print("T," + ",".join(str(v) for v in (t.ticker, t.source, t.side, round(t.opened_ts, 1), round(t.closed_ts or 0, 1), round(t.entry_price, 4), round(t.exit_price, 4), round(t.contracts, 2), round(t.cost, 2),
                                           round(t.pnl, 2), round(t.fees, 2), t.exit_type, t.result or "", int(t.close_ts or 0), int(t.reliable))))
print(f"END {n}")
