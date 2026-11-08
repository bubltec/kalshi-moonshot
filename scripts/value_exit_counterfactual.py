"""What the value exit's sales would have returned without it (2026-11-08, user: "it's only been on a few hours, you're saying it saved 26.51?").
Question: for each live bet the value exit sold since 07:00 ET today, compare what it netted after both fees (sale less the position's cost, stake_usd) with (a) holding to the result and (b) holding with the lane's shock exit running (the replay of watcher._shock_hit: worst 60 s move over 120 s of at least 1.0 typical minute against the bet, cushion within 0.6 typical minutes, last 300 s, bid 0.05-0.89, first second it holds, the sale at the displayed bid less the fee). (b) is what the lane would have done had the value exit not sold the position.
Not covered: the entry-time checks, the cross exit (off on the lane), real fills and slippage, bets still open, and that these 20-odd bets are one afternoon.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/value_exit_counterfactual.py
"""

import bisect
import collections
import itertools
import math

from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
rows = st.db.execute(
    "SELECT d.ts,d.ticker,d.lane,d.side,d.contracts,d.stake_usd,d.exit_ts,d.exit_price,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
    "WHERE d.action='bet' AND d.mode='trade' AND d.exit_reason LIKE 'value exit%' AND d.exit_ts>? AND w.result IN ('yes','no') AND w.strike>0 ORDER BY d.exit_ts",
    (1794100000,),
).fetchall()
print("value exit sales since 07:00 ET with a settled window:", len(rows))
GRID = {}


def grid(ix, a, b):
    r = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND ts<? AND value>0 ORDER BY ts", (ix, a - 1100, b + 5)).fetchall()
    ts = [float(x[0]) for x in r]
    vs = [float(x[1]) for x in r]
    g, j, last = [], 0, None
    for t in range(int(a) - 1000, int(b) + 2):
        while j < len(ts) and ts[j] <= t:
            last = vs[j]
            j += 1
        g.append(math.log(last) if last else None)
    sq = [0.0]
    for x, y in itertools.pairwise(g):
        sq.append(sq[-1] + ((y - x) ** 2 if x is not None and y is not None else 0.0))
    return int(a) - 1000, g, sq


def shock_sale(r):
    ix = variants.crypto_index(r["ticker"].split("-")[0])
    close, K = float(r["close_ts"]), math.log(float(r["strike"]))
    g0, g, sq = grid(ix, float(r["ts"]), close)
    sign = 1.0 if r["side"] == "yes" else -1.0
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], float(r["ts"]) - 20, close)).fetchall()
    qt = [float(x[0]) for x in q]
    n = float(r["contracts"])
    moves = collections.deque()
    for t in range(int(r["ts"]), int(close) - 5):
        k = t - g0
        if k < 960 or k >= len(g) or g[k] is None or g[k - 60] is None:
            continue
        moves.append((t, (g[k] - g[k - 60]) * sign))
        while moves and t - moves[0][0] > 120:
            moves.popleft()
        if close - t > 300:
            continue
        rate = math.sqrt((sq[k] - sq[k - 900]) / 900)
        if rate <= 0:
            continue
        unit = rate * math.sqrt(60.0)
        if not (min(m for _, m in moves) <= -unit and (g[k] - K) * sign <= 0.6 * unit):
            continue
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            continue
        bid = float(q[i][1]) if r["side"] == "yes" else 1.0 - float(q[i][2])
        if 0.05 <= bid <= 0.89:
            return n * (bid - fee(bid))
    return None


tot = {"sold": 0.0, "held": 0.0, "shock": 0.0}
print(f"{'bet':26s} {'sold':>7s} {'held':>7s} {'held+shock':>10s}")
for r in rows:
    n, cost = float(r["contracts"]), float(r["stake_usd"] or 0)
    sold = n * (float(r["exit_price"]) - fee(float(r["exit_price"]))) - cost
    held = (n if r["result"] == r["side"] else 0.0) - cost
    sh = shock_sale(r)
    held_shock = (sh if sh is not None else (n if r["result"] == r["side"] else 0.0)) - cost
    tot["sold"] += sold
    tot["held"] += held
    tot["shock"] += held_shock
    print(f"{r['ticker'][:26]:26s} {sold:+7.2f} {held:+7.2f} {held_shock:+10.2f}" + ("  (shock exit would have sold)" if sh is not None else ""))
print(f"\ntotals over {len(rows)} bets: value exit sold {tot['sold']:+.2f} | held to the result {tot['held']:+.2f} | held with the shock exit {tot['shock']:+.2f}")
print("DONE")
