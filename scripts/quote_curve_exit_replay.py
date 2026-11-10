"""An exit read from the contract's own curve (2026-11-10, user: "it is literally what i thought the curve was"; the treasury markets have no spot feed, so the market's own quote stream is the only curve there).
Question: on the treasury_favorite lane's settled live bets, does selling when the contract's own price curve moves against the bet by N typical moves beat holding to settlement and the lane's own exits? Fee on both legs.
Reading: per second, the side's mid m_t (carried forward from the last quote); z_t = Phi^-1(m_t) (clipped to 0.02-0.98); u_t = z_t * sqrt(seconds left). If the underlying is a diffusion, u is a random walk whatever the volatility, so a move of u over 60 s is in the market's own units. The typical move is the rms of 60 s changes of u pooled over that series' windows that closed in the 6 hours before the bet (rule 2: the market's own recent history, per series by what its curve did, not by name). Sell when the 60 s change of u against the bet is at least N typical moves (and persistence seconds), at the next second's bid less the fee.
Variants (grid fixed before results; any choice after seeing them is said to be so): N 1.0 / 1.5 / 2.0 / 3.0; persistence 0 / 5 s; last-seconds guard 8 s. Result vs holding in two halves. Run: scripts/cloud.sh run scripts/quote_curve_exit_replay.py
"""

import bisect
import math
import statistics

from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee

s = config.reload()
st = Store(s.db_path, s.exclude_versions)
tape = db.connect(s.tape_path, "tape", shared=True)
nd = statistics.NormalDist()
LANE = "treasury_favorite"


def u_grid(ticker, close, side):
    q = tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND bid>0 AND ask>0 AND ts<=? ORDER BY ts", (ticker, close)).fetchall()
    if len(q) < 20:
        return None
    qt = [float(x[0]) for x in q]
    t0 = int(qt[0])
    out = []
    for t in range(t0, int(close) + 1):
        i = bisect.bisect_right(qt, t) - 1
        yb, ya = float(q[i][1]), float(q[i][2])
        bid = yb if side == "yes" else 1 - ya
        mid = (yb + ya) / 2 if side == "yes" else 1 - (yb + ya) / 2
        z = nd.inv_cdf(min(max(mid, 0.02), 0.98))
        out.append((t, z * math.sqrt(max(close - t, 5.0)), bid))
    return out


rows = st.db.execute(
    "SELECT d.ts,d.ticker,d.side,d.contracts,d.stake_usd,w.close_ts,w.result FROM decisions d JOIN windows w ON w.ticker=d.ticker "
    "WHERE d.action='bet' AND d.mode='trade' AND d.lane=? AND d.contracts>0 AND d.stake_usd>0 AND w.result IN ('yes','no') ORDER BY d.ts",
    (LANE,),
).fetchall()
seen = {}
for r in rows:
    seen[r["ticker"]] = r
rows = list(seen.values())

# pooled 60 s changes of u per series, by window close
pool = {}


def window_moves(ticker, close):
    g = u_grid(ticker, close, "yes")
    if not g:
        return []
    return [(g[k][0], (g[k][1] - g[k - 60][1]) ** 2) for k in range(60, len(g))]


for ser in {r["ticker"].split("-")[0] for r in rows}:
    ws = tape.execute("SELECT DISTINCT ticker FROM quotes WHERE ticker LIKE ?", (ser + "-%",)).fetchall()
    pool[ser] = []
    for (tk,) in [tuple(x) for x in ws]:
        try:
            close = st.db.execute("SELECT close_ts FROM windows WHERE ticker=?", (tk,)).fetchone()
        except Exception:
            close = None
        if close:
            pool[ser].append((float(close[0]), window_moves(tk, float(close[0]))))

VARIANTS = [(n, p) for n in (1.0, 1.5, 2.0, 3.0) for p in (0, 5)]
res = {v: [] for v in VARIANTS}
held_all = []
for r in rows:
    ser, close, t0 = r["ticker"].split("-")[0], float(r["close_ts"]), float(r["ts"])
    g = u_grid(r["ticker"], close, r["side"])
    if not g:
        continue
    sq = [m for c, ms in pool[ser] if t0 - 6 * 3600 <= c <= t0 for _, m in ms]
    if len(sq) < 600:
        continue
    typ = math.sqrt(sum(sq) / len(sq))
    n, cost = float(r["contracts"]), float(r["stake_usd"])
    hold = (n if r["result"] == r["side"] else 0.0) - cost
    held_all.append((t0, hold))
    ts_ = [x[0] for x in g]
    start = bisect.bisect_left(ts_, int(t0)) if int(t0) in ts_ else 0
    for v in VARIANTS:
        N, persist = v
        since, pnl = None, hold
        for k in range(max(start, 60), len(g) - 1):
            t, u, bid = g[k]
            if close - t < 8 or bid < 0.05 or t < t0:
                since = None
                continue
            move = u - g[k - 60][1]
            if move <= -N * typ:
                since = since if since is not None else t
                if t - since >= persist:
                    sell = g[k + 1][2]
                    pnl = n * (sell - fee(sell)) - cost
                    break
            else:
                since = None
        res[v].append((t0, pnl, hold, pnl != hold))

print(f"{LANE}: settled bets replayed {len(held_all)}; held to settlement {sum(h for _, h in held_all):+.2f}")
mid = sorted(t for t, _ in held_all)[len(held_all) // 2] if held_all else 0
for v in VARIANTS:
    r = res[v]
    if not r:
        continue
    d = [p - h for _, p, h, _ in r]
    sold = sum(1 for x in r if x[3])
    h1 = sum(p - h for t, p, h, _ in r if t < mid)
    h2 = sum(p - h for t, p, h, _ in r if t >= mid)
    saved = sum(1 for _, p, h, x in r if x and p > h)
    cut = sum(1 for _, p, h, x in r if x and p < h)
    print(f"  N {v[0]:.1f} persist {v[1]}s: net {sum(p for _, p, _, _ in r):+.2f} (vs holding {sum(d):+.2f}) on {sold} sold | beat holding {saved}, lost to holding {cut} | halves {h1:+.2f} / {h2:+.2f}")
print("(the lane's own live exits' result is in the decisions/orders tables; compare there)")
