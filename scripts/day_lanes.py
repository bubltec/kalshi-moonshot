"""Which lanes bet on one day, and what did each make? (2026-11-12, user: "lets just see what lanes we had on with what settings on friday")
The settled bets in `decisions` on one ET day (DAY=YYYY-MM-DD, default 2026-11-06, last Friday), by lane and mode (labels): bets, dollars staked, as traded, held to settlement, how many made and lost money, the mean ask, the share sold before settlement, and the first and last buy of the day. No rule, no model.
    scripts/cloud_job.sh scripts/day_lanes.py DAY=2026-11-06
"""

import collections
import datetime as dt
import os
import statistics
from zoneinfo import ZoneInfo

from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
ET = ZoneInfo("America/New_York")
day = dt.date.fromisoformat(os.environ.get("DAY", "2026-11-06"))
t0 = dt.datetime(day.year, day.month, day.day, tzinfo=ET).timestamp()
store = Store(s.db_path, s.exclude_versions)
rows = store.db.execute("SELECT ts,mode,lane,side,ask,stake_usd,outcome,exit_price,version FROM decisions WHERE action='bet' AND ts>=? AND ts<? AND outcome IS NOT NULL AND stake_usd>0 AND ask>0 ORDER BY ts", (t0, t0 + 86400)).fetchall()
G = collections.defaultdict(list)
for r in rows:
    ask, stake = float(r["ask"]), float(r["stake_usd"])
    n = stake / (ask + fee(ask))
    won = 1.0 if r["outcome"] == (r["side"] or "yes") else 0.0
    xp = r["exit_price"]
    G[(r["mode"], r["lane"])].append((stake, n * ((float(xp) - fee(float(xp))) if xp is not None else won) - stake, n * won - stake, ask, xp is not None, float(r["ts"]), r["version"]))
hm = lambda t: dt.datetime.fromtimestamp(t, ET).strftime("%H:%M")  # noqa: E731
print(f"{len(rows)} settled bets on {day} ET")
for mode in sorted({k[0] for k in G}):
    print(f"\nmode {mode}")
    for (_, lane), xs in sorted(((k, v) for k, v in G.items() if k[0] == mode), key=lambda kv: -sum(x[0] for x in kv[1])):
        print(
            f"  {lane!s:28s} n={len(xs):4d} | staked {sum(x[0] for x in xs):8.2f} | as traded {sum(x[1] for x in xs):+8.2f} | held {sum(x[2] for x in xs):+8.2f} | made money {sum(1 for x in xs if x[1] > 0)}, lost {sum(1 for x in xs if x[1] < 0)}"
            f" | mean ask {statistics.fmean(x[3] for x in xs):.2f} | sold early {sum(1 for x in xs if x[4]) / len(xs):.0%} | buys {hm(min(x[5] for x in xs))}-{hm(max(x[5] for x in xs))} | versions {len({x[6] for x in xs})}"
        )
print("DONE")
