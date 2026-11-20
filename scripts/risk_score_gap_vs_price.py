"""How far must the risk score's chance sit above Kalshi's price before a side pays? (crypto_risk_score, since the 11-18 18:31 ET push)
Question (user, 2026-11-19: "what is our point of risk that needs to show a difference between kalshi's price so we can make a judgement"): per side, gap = the score's chance (1 - its largest risk of the three looks) minus the ask minus the fee; the realized net per dollar held to the result, fee in, by gap bucket, on every eligible side (bought or turned down, one reading per ticker and side, the first the lane wrote, inside 0.60-0.85). If the score has information, the net rises with the gap and crosses zero somewhere; if it is the price echoed, the net is flat across the gap.
Run: scripts/cloud.sh run scripts/risk_score_gap_vs_price.py"""
import collections, os, re
from kalshi_moonshot import config, db
from kalshi_moonshot.strategy import fee_per_contract as fee
s = config.reload(); st = db.connect(s.db_path, "main", shared=True)
SINCE = float(os.environ.get("SINCE", 1795044660))
res = {r["ticker"]: r["result"] for r in st.execute("SELECT ticker, result FROM windows WHERE result IN ('yes','no')").fetchall()}
first = {}
for r in st.execute("SELECT ticker, side, ask, reason, stake_usd FROM decisions WHERE lane='crypto_risk_score' AND ts > ? ORDER BY ts", (SINCE,)).fetchall():
    k = (r["ticker"], r["side"]); a = float(r["ask"] or 0)
    if k not in first and 0.60 <= a <= 0.85: first[k] = r
def risk_of(reason, side):
    m = re.search(side.upper() + r" (?:passed every step|out at \w+|alive so far):.*?risk_score (?:yes|NO) \(risk ([\d, %]+)", reason or "")
    v = [int(x) for x in re.findall(r"(\d+)%", m.group(1))] if m else []
    return max(v) if v else None
G = collections.defaultdict(lambda: [0, 0.0, 0, 0.0, 0.0])
for k, r in first.items():
    if res.get(k[0]) not in ("yes", "no"): continue
    rk = risk_of(r["reason"], k[1])
    if rk is None: continue
    ask = float(r["ask"]); gap = (1 - rk / 100.0) - ask - fee(ask); win = res[k[0]] == k[1]
    net = ((1.0 if win else 0.0) - ask - fee(ask)) / ask
    name = "a  gap under 0 (score at or below the price)" if gap < 0 else "b  gap 0 to 0.03" if gap < 0.03 else "c  gap 0.03 to 0.06" if gap < 0.06 else "d  gap 0.06 and over"
    a = G[name]; a[0] += 1; a[1] += net; a[2] += win; a[3] += gap; a[4] += ask
for name in sorted(G):
    a = G[name]; print(f"{name:46s} n {a[0]:4d} | mean gap {a[3]/a[0]:+.3f} | win {a[2]/a[0]:.0%} vs mean ask {a[4]/a[0]:.2f} | net per $1 {a[1]/a[0]:+.3f}")
