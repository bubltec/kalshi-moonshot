"""crypto_risk_score bets by the spot's own drift over the hour before the bet, signed for the side (user, 2026-11-19: "btc has dropped like 10k in a few days, with short bursts").
Question (rule 4, by curve shape): do bets WITH the hour's drift do better than bets AGAINST it, held to the result, fee in. Drift z = (spot now / spot 60 min ago - 1) over sqrt(60) times the rms one-minute log move of the same hour (the market's own typical minute), so a quiet market's small slide counts as much as a wild market's big one; signed +1 for a YES bet, -1 for NO. Bands: against (z<-1), flat (|z|<=1), with (z>1); the 1 is a plain unit, chosen before seeing results. No coin or asset name is used; the spot is the series' own index.
Also prints each index's move over the whole window as a label for the report.
Run: scripts/cloud.sh run scripts/risk_score_by_drift.py"""
import bisect, collections, math, os
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.strategy import fee_per_contract as fee
os.nice(10)
s = config.reload(); st = db.connect(s.db_path, "main", shared=True); tape = db.connect(s.tape_path, "tape", shared=True)
SINCE = float(os.environ.get("SINCE", 1795044660))
res = {r["ticker"]: r["result"] for r in st.execute("SELECT ticker, result FROM windows WHERE result IN ('yes','no')").fetchall()}
cache = {}
def series_ticks(ix, lo, hi):
    r = tape.execute("SELECT ts, value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts", (ix, lo, hi)).fetchall()
    return [float(x[0]) for x in r], [float(x[1]) for x in r]
def drift_z(ix, now):
    ts, vs = series_ticks(ix, now - 3660, now)
    if len(ts) < 600 or ts[-1] < now - 30 or ts[0] > now - 3300: return None
    mins = []; last = None
    for m in range(61):
        j = bisect.bisect_right(ts, now - 3600 + 60 * m) - 1
        if j >= 0: mins.append(vs[j])
    if len(mins) < 50: return None
    rets = [math.log(b / a) for a, b in zip(mins, mins[1:]) if a > 0 and b > 0]
    rms = math.sqrt(sum(x * x for x in rets) / len(rets)) if rets else 0.0
    if rms <= 0: return None
    return math.log(mins[-1] / mins[0]) / (rms * math.sqrt(60))
rows = st.execute("SELECT ticker, series, side, ask, stake_usd, exit_price, ts FROM decisions d LEFT JOIN (SELECT ticker t2, series FROM windows) w ON w.t2=d.ticker WHERE lane='crypto_risk_score' AND stake_usd IS NOT NULL AND ts > ? ORDER BY ts", (SINCE,)).fetchall()
g = collections.defaultdict(lambda: [0, 0.0, 0.0, 0, 0.0]); seen_ix = {}
for r in rows:
    if res.get(r["ticker"]) not in ("yes", "no") or not r["series"]: continue
    ix = variants.crypto_index(r["series"])
    if not ix: continue
    z = drift_z(ix, float(r["ts"]))
    if z is None: continue
    z *= 1.0 if r["side"] == "yes" else -1.0
    ask = float(r["ask"]); win = res[r["ticker"]] == r["side"]; k = float(r["stake_usd"]) / ask
    held = k * ((1.0 if win else 0.0) - ask - fee(ask))
    sold = held if r["exit_price"] is None else k * (float(r["exit_price"]) - ask - fee(ask) - fee(float(r["exit_price"])))
    name = "AGAINST the hour's drift (z < -1)" if z < -1 else "WITH the hour's drift (z > 1)" if z > 1 else "flat hour (|z| <= 1)"
    a = g[name]; a[0] += 1; a[1] += held; a[2] += sold; a[3] += win; a[4] += float(r["stake_usd"])
for name in sorted(g):
    a = g[name]; print(f"{name:36s} n {a[0]:4d} | held {100*a[1]/a[4]:+6.1f}% | as sold {100*a[2]/a[4]:+6.1f}% | win {a[3]/a[0]:.0%}")
import time
for ser in ("KXBTC15M", "KXETH15M", "KXSOL15M"):
    ix = variants.crypto_index(ser)
    ts, vs = series_ticks(ix, time.time() - 4 * 86400, time.time())
    if ts: print(f"[label] {ser[2:5]} spot: {vs[0]:.4g} -> {vs[-1]:.4g} over {(ts[-1]-ts[0])/86400:.1f} days ({100*(vs[-1]/vs[0]-1):+.1f}%)")
