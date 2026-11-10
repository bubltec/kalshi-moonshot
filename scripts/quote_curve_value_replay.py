"""The curve value of holding, read from Kalshi's own price over time (2026-11-10, user: "all we want is to analyze it based on the spot (kalshi's price) over time").
The contract's mid m_t is the spot and 0.5 is the strike: a contract settles at 1 if its price ends above one half. Chance the side pays: P = Phi((m_t - 0.5) / (r * sqrt(s))) for the side's own mid, r the rms of 60 s changes of the mid over the last W seconds of that contract divided by sqrt(60), s the seconds left. A fast-moving price lowers P, a steady one raises it, independent of where the price sits (so it can differ from the mid, which the z-space shock replay could not).
Sell when the bid net of the fee is at least P + margin, held persist seconds, fill at the next second's bid, fee on both legs; compared with holding to settlement on the treasury_favorite lane's settled bets. Windows W 300 / 600 s (the contract exists only 15 minutes; needs 300 s of history); margins 0.06 / 0.15 / 0.25; persistence 5 s; guard 8 s. Grid fixed before results. Run: scripts/cloud.sh run scripts/quote_curve_value_replay.py
"""

import bisect
import math

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
LANE = "treasury_favorite"


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


rows = st.db.execute(
    "SELECT d.ts,d.ticker,d.side,d.contracts,d.stake_usd,w.close_ts,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
    "WHERE d.action='bet' AND d.mode='trade' AND d.lane=? AND d.contracts>0 AND d.stake_usd>0 AND w.result IN ('yes','no') ORDER BY d.ts",
    (LANE,),
).fetchall()
seen = {}
for r in rows:
    seen[r["ticker"]] = r
rows = list(seen.values())
V = [(w, m, 5) for w in (300, 600) for m in (0.06, 0.15, 0.25)]
res = {v: [] for v in V}
for r in rows:
    close, t0, side = float(r["close_ts"]), float(r["ts"]), r["side"]
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND bid>0 AND ask>0 AND ts<=? ORDER BY ts", (r["ticker"], close)).fetchall()
    if len(q) < 60:
        continue
    qt = [float(x[0]) for x in q]
    base = int(qt[0])
    g = []
    for t in range(base, int(close) + 1):
        i = bisect.bisect_right(qt, t) - 1
        yb, ya = float(q[i][1]), float(q[i][2])
        mid = (yb + ya) / 2 if side == "yes" else 1 - (yb + ya) / 2
        bid = yb if side == "yes" else 1 - ya
        g.append((t, mid, bid))
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    for v in V:
        W, margin, persist = v
        since, pnl = None, hold
        for k in range(60, len(g) - 1):
            t, m, bid = g[k]
            if t < t0 or close - t < 8 or bid < 0.05 or k < W // 2 + 60:
                since = None
                continue
            lo = max(60, k - W)
            inc = [(g[j][1] - g[j - 60][1]) ** 2 / 60.0 for j in range(lo, k + 1)]
            rate = math.sqrt(sum(inc) / len(inc))
            c = m - 0.5
            p = phi(c / (rate * math.sqrt(close - t))) if rate > 0 else (1.0 if c > 0 else 0.0)
            over = bid - fee(bid) - p
            if over >= margin:
                since = since if since is not None else t
                if t - since >= persist:
                    sell = g[k + 1][2]
                    pnl = n * (sell - fee(sell)) - cost
                    break
            else:
                since = None
        res[v].append((t0, pnl, hold, pnl != hold))

held = sum(h for _, _, h, _ in res[V[0]])
print(f"{LANE}: settled bets replayed {len(res[V[0]])}; held to settlement {held:+.2f}")
mid_t = sorted(t for t, _, _, _ in res[V[0]])[len(res[V[0]]) // 2]
for v in V:
    r = res[v]
    d = [p - h for _, p, h, _ in r]
    sold = sum(1 for x in r if x[3])
    h1 = sum(p - h for t, p, h, _ in r if t < mid_t)
    h2 = sum(p - h for t, p, h, _ in r if t >= mid_t)
    saved = sum(1 for _, p, h, x in r if x and p > h)
    cut = sum(1 for _, p, h, x in r if x and p < h)
    print(f"  window {v[0]}s margin {v[1]:.2f}: net {sum(p for _, p, _, _ in r):+.2f} (vs holding {sum(d):+.2f}) on {sold} sold | beat holding {saved}, lost to holding {cut} | halves {h1:+.2f} / {h2:+.2f}")
