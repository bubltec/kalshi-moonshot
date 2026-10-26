"""TREND IN UNITS OF THE MARKET'S OWN NOISE: for a lane's finished bets, the move of the bought side's mid over the last W seconds (5 or 10) divided by that market's own typical move over W seconds (the standard deviation of its W-second changes over the previous 2 minutes), so a 2c move on a quiet market counts for more than on a jumpy one. Groups by that z and the return of each (after the buy fee; exit or result), errors clustered by window, then 'keep only z >= x' rules.
    docker compose exec -T bot python - < scripts/entry_zscore_study.py   (LANE=crypto_favorite SINCE_DAYS=7)
"""
import bisect
import math
import os
import statistics
from collections import defaultdict

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
lane = os.environ.get("LANE", "crypto_favorite")
days = float(os.environ.get("SINCE_DAYS", "7"))
rows = st.db.execute("""SELECT d.ts, d.ticker, d.side, d.ask, d.exit_price, w.result, w.close_ts FROM decisions d JOIN windows w ON w.ticker=d.ticker
    WHERE d.lane=? AND d.action='bet' AND w.result IN ('yes','no') AND d.ts > extract(epoch from now()) - ?*86400""", (lane, days)).fetchall()
bets = []
for r in rows:
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts<=? AND ts>? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], r["ts"], r["ts"] - 150)).fetchall()
    if len(q) < 6:
        continue
    ts = [x[0] for x in q]
    mid = [((x[1] + x[2]) / 2 if r["side"] == "yes" else 1 - (x[1] + x[2]) / 2) for x in q]

    def at(t, ts=ts, mid=mid):
        i = bisect.bisect_right(ts, t) - 1
        return mid[i] if i >= 0 else None
    z = {}
    for W in (5, 10):
        now_mid, then = at(r["ts"]), at(r["ts"] - W)
        grid = [at(r["ts"] - 5 - k) for k in range(0, 110)]
        steps = [a - b for a, b in zip(grid[:-W], grid[W:], strict=False) if a is not None and b is not None]
        sd = statistics.pstdev(steps) if len(steps) > 20 else 0.0
        z[W] = (now_mid - then) / sd if (now_mid is not None and then is not None and sd > 1e-6) else None
    won = 1.0 if r["result"] == r["side"] else 0.0
    got = r["exit_price"] if r["exit_price"] is not None else won
    ask = float(r["ask"])
    bets.append({"win": r["close_ts"], "z": z, "pnl": got - ask - fee(ask), "cost": ask + fee(ask)})


def agg(g):
    wins = defaultdict(float)
    for b in g:
        wins[b["win"]] += b["pnl"]
    tot, cost = sum(b["pnl"] for b in g), sum(b["cost"] for b in g)
    return tot, cost, (math.sqrt(len(wins)) * statistics.pstdev(list(wins.values())) if len(wins) > 1 else float("nan"))


print(lane, len(bets), "bets with quotes; z available:", {W: sum(1 for b in bets if b["z"][W] is not None) for W in (5, 10)})
for W in (5, 10):
    print(f"z over {W} s")
    for name, lo, hi in (("z < -1.5", -99, -1.5), ("-1.5 .. -0.5", -1.5, -0.5), ("-0.5 .. 0.5", -0.5, 0.5), ("0.5 .. 1.5", 0.5, 1.5), ("z >= 1.5", 1.5, 99)):
        g = [b for b in bets if b["z"][W] is not None and lo <= b["z"][W] < hi]
        if len(g) >= 8:
            t, c, se = agg(g)
            print(f"  {name:14} {len(g):4} bets; net {t:+7.2f} ({100 * t / c:+6.1f}% of cost, +-{se:.2f})")
    for x in (-1.0, -0.5, 0.0, 0.5, 1.0):
        keep = [b for b in bets if b["z"][W] is not None and b["z"][W] >= x]
        if len(keep) >= 20:
            t, c, se = agg(keep)
            print(f"  keep z >= {x:+.1f}: {len(keep):3} bets; net {t:+7.2f} ({100 * t / c:+6.1f}% of cost, +-{se:.2f})")
