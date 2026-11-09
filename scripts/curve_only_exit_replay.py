"""Going by the curve alone for exits (2026-11-08, user: "when you say 'the curve said hold' why aren't we just going by the curve??"; HYPE 21:45 NO bought 0.68, sold 0.56 by the shock exit at the first second of its window while the curve's chance (0.62) was over the bid (0.57)).
Today three rules decide a sale: the shock exit reads the spot's move and cushion in typical minutes, the value exit reads confidence.py's chance (a logistic on the curve features and the price) and only sells a gain, and nothing uses the chance to HOLD against the shock exit. Question: if one number decided, the confidence model's chance that the side pays, selling whenever the bid after the fee is over that chance by a margin M (the bid overvalues the side; any bid between 0.05 and 0.95, at a loss or a gain), what would the live crypto_multiscale bets since 11-05 have returned, against holding to the result, against the shock exit alone (replayed), and against what happened?
Replay every 5 s from the bet to 5 s before the close: chance from confidence.features_at on the tape's index ticks and the quote of that second (the bid is the held side's bid, the ask the bid plus a cent), over = bid - fee - chance, sale the first second over >= M, at the bid less the fee. M in 0.02, 0.03, 0.05, 0.08 is a grid. The model is the current one (fitted on days including these: partly in-sample, said here; its Brier score against the ask is 0.1225 vs 0.1202, i.e. no better than the market price).
Reported: bets, net in dollars of each policy, the curve-only policy's difference from holding and from the shock exit alone with a standard error per bet, how many bets it sells, and the two halves.
Not covered: the lane's real fills, the value exit's own band and guard (this policy has neither), the combination (curve-only plus the shock exit).

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/curve_only_exit_replay.py
"""

import bisect
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
MS = (0.02, 0.03, 0.05, 0.08)
STEP = 5
rows = st.db.execute(
    "SELECT d.ts,d.ticker,d.side,d.contracts,d.stake_usd,w.close_ts,w.strike,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
    "WHERE d.action='bet' AND d.mode='trade' AND d.lane='crypto_multiscale' AND d.contracts>0 AND d.stake_usd>0 AND d.ts>? AND w.result IN ('yes','no') AND w.strike>0 ORDER BY d.ts",
    (1794000000,),
).fetchall()
print("live crypto_multiscale bets with a settled window since 11-05:", len(rows), "| model:", "loaded" if model else "MISSING")
res = []
for r in rows:
    ix = variants.crypto_index(r["ticker"].split("-")[0])
    if not ix or not model:
        continue
    close, t0 = float(r["close_ts"]), float(r["ts"])
    tk = tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts BETWEEN ? AND ? AND value>0 ORDER BY ts", (ix, t0 - 3700, close)).fetchall()
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts", (r["ticker"], t0 - 5, close)).fetchall()
    if len(tk) < 500 or len(q) < 5:
        continue
    T = [float(x[0]) for x in tk]
    V = [float(x[1]) for x in tk]
    qt = [float(x[0]) for x in q]
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    sold = {}
    t = t0 + 1.0
    while t < close - 5 and len(sold) < len(MS):
        i = bisect.bisect_right(qt, t) - 1
        if i >= 0 and t - qt[i] < 10:
            yb, ya = float(q[i][1]), float(q[i][2])
            bid = yb if r["side"] == "yes" else 1.0 - ya
            if 0.05 <= bid <= 0.95:
                ask = min(0.99, bid + 0.01)
                yes_bid, yes_ask = (bid, ask) if r["side"] == "yes" else (max(0.01, 1.0 - ask), 1.0 - bid)
                f = confidence.features_at(T, V, t, float(r["strike"]), close - t, yes_bid, yes_ask)
                if f is not None:
                    p_yes = confidence.predict(model["w"], confidence.vector(f, model["mu"], model["sd"]))
                    chance = p_yes if r["side"] == "yes" else 1.0 - p_yes
                    over = bid - fee(bid) - chance
                    for m in MS:
                        if m not in sold and over >= m:
                            sold[m] = n * (bid - fee(bid)) - cost
        t += STEP
    res.append((t0, hold, {m: sold.get(m, hold) for m in MS}, len(sold) and {m: (m in sold) for m in MS}))
print("replayed:", len(res))
mid = sorted(x[0] for x in res)[len(res) // 2]


def ms(xs):
    n = len(xs)
    m = sum(xs) / n
    se = (sum((x - m) ** 2 for x in xs) / max(n - 1, 1)) ** 0.5 / math.sqrt(n)
    return f"{m:+.3f} +/- {se:.3f}"


print(f"\nheld to the result: net ${sum(x[1] for x in res):+.2f} over {len(res)} bets")
for m in MS:
    nets = [x[2][m] for x in res]
    d = [x[2][m] - x[1] for x in res]
    a = [x[2][m] - x[1] for x in res if x[0] <= mid]
    b = [x[2][m] - x[1] for x in res if x[0] > mid]
    nsold = sum(1 for x in res if x[3] and x[3].get(m))
    print(f"  curve-only, sell when the bid is {m:.2f} over the chance: net ${sum(nets):+8.2f} | against holding {sum(d):+.2f} (per bet {ms(d)}; halves {sum(a):+.2f} / {sum(b):+.2f}) | sells {nsold} of {len(res)} bets")
print("DONE")
