"""BUY ONLY WHEN THE SIDE IS TRENDING OUR WAY: for a lane's finished bets, the change in the bought side's price (the mid of its bid and ask, in its own terms) over the 10, 20, 30 and 60 seconds before the buy, and what each trend group returned (after the buy fee; held to the result or the exit). Errors clustered by window. Also each bet's best simple rule: skip when the trend over N seconds is under X.
    docker compose exec -T bot python - < scripts/entry_trend_study.py   (LANE=crypto_favorite SINCE_DAYS=7)
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
LOOK = (10, 20, 30, 60)
bets = []
for r in rows:
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts<=? AND ts>? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], r["ts"], r["ts"] - 120)).fetchall()
    if len(q) < 3:
        continue
    ts = [x[0] for x in q]
    mid = [((x[1] + x[2]) / 2 if r["side"] == "yes" else 1 - (x[1] + x[2]) / 2) for x in q]
    now_mid = mid[-1]
    tr = {}
    for n in LOOK:
        i = bisect.bisect_right(ts, r["ts"] - n) - 1
        tr[n] = now_mid - mid[i] if i >= 0 else None
    won = 1.0 if r["result"] == r["side"] else 0.0
    got = r["exit_price"] if r["exit_price"] is not None else won
    ask = float(r["ask"])
    bets.append({"win": r["close_ts"], "tr": tr, "pnl": got - ask - fee(ask), "cost": ask + fee(ask)})


def agg(g):
    wins = defaultdict(float)
    for b in g:
        wins[b["win"]] += b["pnl"]
    tot, cost = sum(b["pnl"] for b in g), sum(b["cost"] for b in g)
    se = math.sqrt(len(wins)) * statistics.pstdev(list(wins.values())) if len(wins) > 1 else float("nan")
    return tot, cost, se


print(lane, len(bets), "bets with quotes")
for n in LOOK:
    print(f"trend over the last {n} s (cents)")
    for name, lo, hi in (("fell 2c+", -9, -0.02), ("fell 0.5-2c", -0.02, -0.005), ("flat (+-0.5c)", -0.005, 0.005), ("rose 0.5-2c", 0.005, 0.02), ("rose 2c+", 0.02, 9)):
        g = [b for b in bets if b["tr"][n] is not None and lo <= b["tr"][n] < hi]
        if len(g) >= 8:
            t, c, se = agg(g)
            print(f"  {name:14} {len(g):4} bets; net {t:+7.2f} ({100 * t / c:+6.1f}% of cost, se of the sum +-{se:.2f})")
print("rule: skip when the trend over N s is under X (what is left)")
for n in LOOK:
    for x in (-0.01, 0.0, 0.01):
        keep = [b for b in bets if b["tr"][n] is not None and b["tr"][n] >= x]
        if len(keep) >= 20:
            t, c, se = agg(keep)
            print(f"  N={n:2} X={100 * x:+.0f}c: {len(keep):3} bets kept; net {t:+7.2f} ({100 * t / c:+6.1f}% of cost, +-{se:.2f})")
