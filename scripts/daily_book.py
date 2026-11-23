"""What did the book make each day, and where did it change? (2026-11-12, user: "how were we making good money like last friday and somehow we have ruined everything")
No rule and no model: the settled bets in `decisions` since 2026-10-31, by ET day, in three groups: the bot's real bets (mode trade), the bot's paper bets (mode observe, lane not manual) and the user's hand-placed bets (lane manual). Per day and group: bets, dollars staked, dollars as traded (the real exit, fees in), the same bets held to settlement, and how many made and lost money. Then, for the bot's real bets, before Monday 2026-11-09 (the logic rewrite) against since: by lane and by exit reason, as traded against held, so a change in the entries (held moves) can be told from a change in the exits (the gap between traded and held moves). Lanes and exit reasons are labels. Contracts come from the stake and the recorded ask (decisions.contracts undercounts fills); fees are per contract, unrounded.
    scripts/cloud_job.sh scripts/daily_book.py
"""

import collections
import datetime as dt
from zoneinfo import ZoneInfo

from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
ET = ZoneInfo("America/New_York")
START = dt.datetime(2026, 10, 31, tzinfo=ET).timestamp()
MON = dt.datetime(2026, 11, 9, tzinfo=ET).timestamp()
store = Store(s.db_path, s.exclude_versions)
rows = store.db.execute("SELECT ts,mode,lane,side,ask,stake_usd,outcome,exit_price,exit_reason FROM decisions WHERE action='bet' AND ts>? AND outcome IS NOT NULL AND stake_usd>0 AND ask>0 ORDER BY ts", (START,)).fetchall()
B = []  # (day, group, lane, exit reason, since Monday, stake, as traded, held)
for r in rows:
    ask, stake = float(r["ask"]), float(r["stake_usd"])
    n = stake / (ask + fee(ask))
    won = 1.0 if r["outcome"] == (r["side"] or "yes") else 0.0
    xp = r["exit_price"]
    traded = n * ((float(xp) - fee(float(xp))) if xp is not None else won) - stake
    group = "your manual bets" if r["lane"] == "manual" else ("bot real (trade)" if r["mode"] == "trade" else f"bot paper ({r['mode']})")
    B.append((dt.datetime.fromtimestamp(float(r["ts"]), ET).strftime("%m-%d %a"), group, r["lane"], (r["exit_reason"] or "sold, no reason recorded").split(":")[0].strip() if xp is not None else "held to settlement", float(r["ts"]) >= MON, stake, traded, n * won - stake))


def line(label, xs):
    if not xs:
        return f"  {label:34s} n=0"
    return f"  {label:34s} n={len(xs):5d} | staked {sum(x[5] for x in xs):9.2f} | as traded {sum(x[6] for x in xs):+9.2f} | held {sum(x[7] for x in xs):+9.2f} | exits cost {sum(x[6] - x[7] for x in xs):+9.2f} | made money {sum(1 for x in xs if x[6] > 0)}, lost {sum(1 for x in xs if x[6] < 0)}"


print(f"{len(B)} settled bets since 10-31 ET; the last is on {B[-1][0]}")
for g in sorted({x[1] for x in B}):
    print(f"\n{g}, by day")
    for d in sorted({x[0] for x in B if x[1] == g}):
        print(line(d, [x for x in B if x[1] == g and x[0] == d]))
    print(line("before Monday 11-09", [x for x in B if x[1] == g and not x[4]]))
    print(line("since Monday 11-09", [x for x in B if x[1] == g and x[4]]))
real = [x for x in B if x[1] == "bot real (trade)"]
for what, ix in (("lane", 2), ("exit reason", 3)):
    for label, since in (("before Monday 11-09", False), ("since Monday 11-09", True)):
        print(f"\nthe bot's real bets by {what} (label only), {label}")
        xs = [x for x in real if x[4] == since]
        tot = collections.Counter()
        for x in xs:
            tot[x[ix]] += 1
        for k, _ in tot.most_common(14):
            print(line(str(k)[:34], [x for x in xs if x[ix] == k]))
print("\nthe bot's real bets since Sunday 11-08, by day and exit kind (the text before the colon of the exit reason)")
for d in sorted({x[0] for x in real if x[0] >= "11-08"}):
    print(" " + d)
    for k in sorted({x[3] for x in real if x[0] == d}):
        print(line("  " + str(k)[:32], [x for x in real if x[0] == d and x[3] == k]))
print("DONE")
