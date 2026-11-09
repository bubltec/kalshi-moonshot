"""The exit policies half by half (2026-11-08, user: "the curve in the second half was way profitable, we should make that a point").
Question: on the same live crypto_multiscale bets since 11-05 (settled windows), net in dollars per policy in each half of the period (split by bet time at the median), so the curve-only policy's second half can be set beside what holding, the shock exit alone, and what the lane's exits actually did made in the same bets:
  hold          every bet held to the result.
  shock         the live shock exit's rule replayed alone (worst 60 s move over 120 s at least 1.0 typical minutes against the bet, cushion within 0.6 typical minutes, last 300 s, bid 0.05-0.89, first second it holds), at the bid less the fee.
  curve 0.02/0.03  sell the first 5 s step at which the bid after the fee is over the confidence model's chance by M (bid 0.05-0.95, any gain or loss).
  actual        what the lane really made: the recorded exit price less the fee when it sold, the result otherwise.
Reported per half: bets, the net of each policy, and for curve and shock the net against holding. The model is the current one (partly in-sample, no better than the market price).
Not covered: real fills, the exits running together as they do live, why the second half differs (more of its bets were taken on the day the whole market jumped).

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/exit_policies_by_half.py
"""

import bisect
import collections
import itertools
import math
import os

from kalshi_moonshot import confidence, config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

os.nice(15)
s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
model = confidence.load_model(s)
MS = (0.02, 0.03)
rows = st.db.execute(
    "SELECT d.ts,d.ticker,d.side,d.contracts,d.stake_usd,d.exit_price,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
    "WHERE d.action='bet' AND d.mode='trade' AND d.lane='crypto_multiscale' AND d.contracts>0 AND d.stake_usd>0 AND d.ts>? AND w.result IN ('yes','no') AND w.strike>0 ORDER BY d.ts",
    (1794000000,),
).fetchall()
print("live crypto_multiscale bets with a settled window since 11-05:", len(rows))
out = []
for r in rows:
    ix = variants.crypto_index(r["ticker"].split("-")[0])
    if not ix or not model:
        continue
    close, t0 = float(r["close_ts"]), float(r["ts"])
    K0 = math.log(float(r["strike"]))
    sign = 1.0 if r["side"] == "yes" else -1.0
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, t0 - 3700, close)).fetchall()
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], t0 - 5, close)).fetchall()
    if len(tk) < 1500 or len(q) < 5:
        continue
    T = [float(x[0]) for x in tk]
    V = [float(x[1]) for x in tk]
    qt = [float(x[0]) for x in q]
    base = int(T[0])
    g, j, last = [], 0, None
    for t in range(base, int(close) + 1):
        while j < len(T) and T[j] <= t:
            last = V[j]
            j += 1
        g.append(math.log(last) if last else None)
    sq = [0.0]
    for a, b in itertools.pairwise(g):
        sq.append(sq[-1] + ((b - a) ** 2 if a is not None and b is not None else 0.0))
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    actual = (n * (float(r["exit_price"]) - fee(float(r["exit_price"]))) - cost) if r["exit_price"] else hold
    moves = collections.deque()
    shock = None
    curve = {}
    for t in range(int(t0) + 1, int(close) - 5):
        k = t - base
        if k < 960 or k >= len(g) or g[k] is None or g[k - 60] is None:
            continue
        moves.append((t, (g[k] - g[k - 60]) * sign))
        while moves and t - moves[0][0] > 120:
            moves.popleft()
        i = bisect.bisect_right(qt, t) - 1
        if i < 0 or t - qt[i] > 20:
            continue
        yb, ya = float(q[i][1]), float(q[i][2])
        bid = yb if r["side"] == "yes" else 1.0 - ya
        if shock is None and close - t <= 300 and 0.05 <= bid <= 0.89:
            rate = math.sqrt((sq[k] - sq[k - 900]) / 900)
            if rate > 0:
                unit = rate * math.sqrt(60.0)
                if min(m for _, m in moves) <= -unit and (g[k] - K0) * sign <= 0.6 * unit:
                    shock = n * (bid - fee(bid)) - cost
        if (t - int(t0)) % 5 == 0 and len(curve) < len(MS) and 0.05 <= bid <= 0.95:
            ask = min(0.99, bid + 0.01)
            yes_bid, yes_ask = (bid, ask) if r["side"] == "yes" else (max(0.01, 1.0 - ask), 1.0 - bid)
            f = confidence.features_at(T, V, float(t), float(r["strike"]), close - t, yes_bid, yes_ask)
            if f is not None:
                p_yes = confidence.predict(model["w"], confidence.vector(f, model["mu"], model["sd"]))
                chance = p_yes if r["side"] == "yes" else 1.0 - p_yes
                over = bid - fee(bid) - chance
                for m in MS:
                    if m not in curve and over >= m:
                        curve[m] = n * (bid - fee(bid)) - cost
        if shock is not None and len(curve) == len(MS):
            break
    out.append((t0, hold, actual, shock if shock is not None else hold, {m: curve.get(m, hold) for m in MS}))
print("replayed:", len(out))
mid = sorted(o[0] for o in out)[len(out) // 2]
for label, part in (("first half", [o for o in out if o[0] <= mid]), ("second half", [o for o in out if o[0] > mid]), ("all", out)):
    print(f"\n{label}: {len(part)} bets")
    print(f"   hold {sum(o[1] for o in part):+8.2f} | actual (the lane's exits) {sum(o[2] for o in part):+8.2f} | shock alone {sum(o[3] for o in part):+8.2f} (against holding {sum(o[3] - o[1] for o in part):+.2f})")
    for m in MS:
        print(f"   curve-only M={m:.2f}: {sum(o[4][m] for o in part):+8.2f} (against holding {sum(o[4][m] - o[1] for o in part):+.2f}, against the lane's actual {sum(o[4][m] - o[2] for o in part):+.2f})")
print("DONE")
