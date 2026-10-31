"""Every cashed-out bot trade of the last DAYS days: what the sale made against what holding to settlement would have made. Read-only.
Counterfactual = contracts x (1 if the side won else 0 - entry price), before fees (the sale's fees are in the actual number, so holding looks slightly better than it is).
    scripts/cloud.sh run scripts/cashout_vs_hold.py [DAYS=7] [SINCE_UTC_TS=0]
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
CUT = float(os.environ.get("CUT", "0"))
since = time.time() - DAYS * 86400
res = {r["ticker"]: r["result"] for r in store.db.execute("SELECT ticker, result FROM windows WHERE result IN ('yes','no')")}
rows = []
for t in build_trips(store):
    if not (t.done and t.reliable and t.source == "bot" and t.series.endswith("15M") and t.closed_ts >= since and t.cashed_out > 1e-9 and t.ticker in res):
        continue
    won = res[t.ticker] == t.side
    hold = t.contracts * ((1.0 if won else 0.0) - t.entry_price)
    rows.append((t, won, hold))
for label, sub in (("before the cut", [r for r in rows if r[0].closed_ts < CUT]), ("after the cut", [r for r in rows if r[0].closed_ts >= CUT]), ("all", rows)):
    if not sub:
        continue
    print(f"{label:<15} {len(sub):>3} cash-outs; the side then WON {sum(1 for r in sub if r[1])}; sale made {sum(r[0].pnl for r in sub):+.2f}, holding would have made about {sum(r[2] for r in sub):+.2f}")
by = defaultdict(list)
for r in rows:
    by[re.match(r"^KX([A-Z]+)15M$", r[0].series).group(1)].append(r)
print("\nby commodity (sale / hold / sides that won anyway):")
for k, v in sorted(by.items(), key=lambda kv: sum(r[0].pnl - r[2] for r in kv[1])):
    print(f"  {k:<10} n {len(v):>2}  sale {sum(r[0].pnl for r in v):>+7.2f}  hold {sum(r[2] for r in v):>+7.2f}  won anyway {sum(1 for r in v if r[1])}")
