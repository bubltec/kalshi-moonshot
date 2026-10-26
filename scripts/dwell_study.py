"""BUYING THE MOMENT IT HITS THE BAND: for a lane's finished bets, how long had the bought side already been at or above 0.70 when we bought (seconds of dwell, from the tape's quotes), and what did buying later vs right at the crossing return. Also: where the ask was 60 s earlier (a fresh jump up vs a slow climb). Per-dollar return after the buy fee, held to the result or the exit; errors clustered by window.
    docker compose exec -T bot python - < scripts/dwell_study.py   (LANE=crypto_favorite SINCE_DAYS=7)
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
LO = 0.70
bets = []
for r in rows:
    q = tape.execute("SELECT ts, bid, ask FROM quotes WHERE ticker=? AND ts<=? AND ts>? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], r["ts"], r["ts"] - 900)).fetchall()
    if len(q) < 5:
        continue
    px = [(x[0], x[2] if r["side"] == "yes" else 1 - x[1]) for x in q]  # the price we would pay for the side
    ts = [p[0] for p in px]
    dwell = 0.0
    for t, p in reversed(px):  # back from the buy to the last time it was under the band
        if p < LO:
            dwell = r["ts"] - t
            break
    else:
        dwell = r["ts"] - px[0][0]
    i = bisect.bisect_right(ts, r["ts"] - 60) - 1
    ago = px[i][1] if i >= 0 else None
    won = 1.0 if r["result"] == r["side"] else 0.0
    got = r["exit_price"] if r["exit_price"] is not None else won
    ask = float(r["ask"])
    bets.append({"win": r["close_ts"], "dwell": dwell, "ago": ago, "ask": ask, "pnl": got - ask - fee(ask), "cost": ask + fee(ask)})


def show(name, groups):
    print(name)
    for key, g in groups:
        if len(g) < 8:
            continue
        wins = defaultdict(float)
        for b in g:
            wins[b["win"]] += b["pnl"]
        tot, cost = sum(b["pnl"] for b in g), sum(b["cost"] for b in g)
        se = math.sqrt(len(wins)) * statistics.pstdev(list(wins.values())) if len(wins) > 1 else float("nan")
        print(f"  {key:22} {len(g):4} bets; net {tot:+7.2f} ({100 * tot / cost:+6.1f}% of cost, cluster se of the sum +-{se:.2f})")


print(lane, len(bets), "bets with quotes")
show("seconds the side had already been at or above 0.70 when bought", [(f"{a}-{b} s", [x for x in bets if a <= x["dwell"] < b]) for a, b in ((0, 10), (10, 30), (30, 90), (90, 300), (300, 1000))])
show("the price 60 s before the buy", [(name, [x for x in bets if x["ago"] is not None and lo <= x["ago"] - x["ask"] < hi]) for name, lo, hi in (("was 15c+ lower (jump)", -1, -0.15), ("was 5-15c lower", -0.15, -0.05), ("within 5c", -0.05, 0.05), ("was higher (falling)", 0.05, 1))])
