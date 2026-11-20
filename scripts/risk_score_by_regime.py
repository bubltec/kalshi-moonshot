"""crypto_risk_score bets by the spot's volatility regime at the bet: its typical minute over the last 30 min against its own median 30-minute typical minute over the day before (CLAUDE.md rule 3: a reading beyond 3x its own median is an anomaly, a state of its own).
Question (user, 2026-11-19: crypto losses today came in short bursts as the whole market went down): do bets placed in a high-volatility regime (ratio over 1.5, over 3) lose more than the rest, held and as sold, fee in. Units: the index's own rms one-minute log move; no coin or asset name is used. Cutoffs 1.5 and 3 are plain units set before seeing results (3 is rule 3's).
Run: scripts/cloud.sh run scripts/risk_score_by_regime.py"""
import bisect, collections, math, os
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.strategy import fee_per_contract as fee
os.nice(10)
s = config.reload(); st = db.connect(s.db_path, "main", shared=True); tape = db.connect(s.tape_path, "tape", shared=True)
SINCE = float(os.environ.get("SINCE", 1795044660))
res = {r["ticker"]: r["result"] for r in st.execute("SELECT ticker, result FROM windows WHERE result IN ('yes','no')").fetchall()}
bets = st.execute("SELECT d.ticker, w.series, d.side, d.ask, d.stake_usd, d.exit_price, d.ts FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.lane='crypto_risk_score' AND d.stake_usd IS NOT NULL AND d.ts > ? ORDER BY d.ts", (SINCE,)).fetchall()
grids = {}
def minutes(ix):
    if ix in grids: return grids[ix]
    t0 = SINCE - 90000
    r = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts", (ix, t0)).fetchall()
    ts = [float(x[0]) for x in r]; vs = [float(x[1]) for x in r]
    m0 = int(t0 // 60) * 60; out = []; last = None; j = 0
    for m in range(0, int((max(ts) - m0) // 60) + 2):
        T = m0 + 60 * m
        while j < len(ts) and ts[j] <= T: last = vs[j]; j += 1
        out.append(last)
    grids[ix] = (m0, out); return grids[ix]
def rms(seq):
    r = [math.log(b / a) for a, b in zip(seq, seq[1:]) if a and b]
    return math.sqrt(sum(x * x for x in r) / len(r)) if len(r) >= 20 else None
def ratio(ix, now):
    m0, g = minutes(ix); k = int((now - m0) // 60)
    if k < 1500 or k >= len(g): return None
    cur = rms(g[k - 30:k + 1])
    hist = sorted(x for x in (rms(g[e - 30:e + 1]) for e in range(k - 1440, k - 30, 15)) if x)
    if cur is None or len(hist) < 40: return None
    return cur / hist[len(hist) // 2]
G = collections.defaultdict(lambda: [0, 0.0, 0.0, 0, 0.0])
for r in bets:
    if res.get(r["ticker"]) not in ("yes", "no") or not r["series"]: continue
    ix = variants.crypto_index(r["series"])
    if not ix: continue
    q = ratio(ix, float(r["ts"]))
    if q is None: continue
    ask = float(r["ask"]); win = res[r["ticker"]] == r["side"]; k = float(r["stake_usd"]) / ask
    held = k * ((1.0 if win else 0.0) - ask - fee(ask))
    sold = held if r["exit_price"] is None else k * (float(r["exit_price"]) - ask - fee(ask) - fee(float(r["exit_price"])))
    name = "a ratio under 1 (calmer than its median)" if q < 1 else "b ratio 1 to 1.5" if q < 1.5 else "c ratio 1.5 to 3" if q < 3 else "d ratio over 3 (rule 3's anomaly)"
    a = G[name]; a[0] += 1; a[1] += held; a[2] += sold; a[3] += win; a[4] += float(r["stake_usd"])
for name in sorted(G):
    a = G[name]; print(f"{name:42s} n {a[0]:4d} | held {100*a[1]/a[4]:+6.1f}% | as sold {100*a[2]/a[4]:+6.1f}% | win {a[3]/a[0]:.0%}")
