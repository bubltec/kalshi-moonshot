"""Does the risk-score pick beat buying every eligible side? (crypto_risk_score, since the 11-18 18:31 ET push)
Question: on the same markets, held to the result with the fee in, what does a side net per dollar when the lane BOUGHT it, when the risk score TURNED IT DOWN (price inside the lane's band 0.60-0.85, it got past the price step), and when it was anything in the band; and does the score's own number (the largest of its three looks) sort sides into winners and losers.
One reading per (ticker, side): the first row the lane wrote for it (a side that was ever bought counts as bought). Result from the windows table.
Run: scripts/cloud.sh run scripts/risk_score_vs_everything.py"""
import collections, os, re
from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee
s = config.reload()
st = db.connect(s.db_path, "main", shared=True)
SINCE = float(os.environ.get("SINCE", 1795044660))
res = {r["ticker"]: r["result"] for r in st.execute("SELECT ticker, result FROM windows WHERE result IN ('yes','no')").fetchall()}
rows = st.execute("SELECT ticker, side, ask, stake_usd, ts, reason FROM decisions WHERE lane='crypto_risk_score' AND ts > ? ORDER BY ts", (SINCE,)).fetchall()
first, bought = {}, set()
for r in rows:
    k = (r["ticker"], r["side"]); a = float(r["ask"] or 0)
    if r["stake_usd"] is not None: bought.add(k)
    if k not in first and 0.60 <= a <= 0.85 and "out at price" not in (r["reason"] or "").split("|")[0]: first[k] = r
for k, r in first.items():
    if k in bought:
        first[k] = next(x for x in rows if (x["ticker"], x["side"]) == k and x["stake_usd"] is not None)
def risk_of(reason, side):
    m = re.search(side.upper() + r" (?:passed every step|out at \w+|alive so far):.*?risk_score (?:yes|NO) \(risk ([\d, %]+)", reason or "")
    if not m: return None
    v = [int(x) for x in re.findall(r"(\d+)%", m.group(1))]
    return max(v) if v else None
def net(ask, win): return ((1.0 if win else 0.0) - ask - fee(ask)) / ask
g = collections.defaultdict(lambda: [0, 0.0, 0])
for k, r in first.items():
    if res.get(k[0]) not in ("yes", "no"): continue
    ask = float(r["ask"]); win = res[k[0]] == k[1]; n = net(ask, win)
    for name in ("every eligible side (price band 0.60-0.85)", "BOUGHT" if k in bought else "TURNED DOWN (not bought)"):
        a = g[name]; a[0] += 1; a[1] += n; a[2] += win
    rk = risk_of(r["reason"], k[1])
    if rk is not None:
        b = "risk <=30%" if rk <= 30 else "risk 31-40%" if rk <= 40 else "risk 41-50%" if rk <= 50 else "risk >50%"
        a = g["by score: " + b]; a[0] += 1; a[1] += n; a[2] += win
for name in sorted(g):
    a = g[name]; print(f"{name:44s} n {a[0]:5d} | net per $1 staked {a[1]/a[0]:+.3f} | win {a[2]/a[0]:.0%}")
