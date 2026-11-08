"""Live crypto_multiscale bets by how the buy filled and by minutes into the window (2026-11-08): are the late losses resting-bid adverse selection?

Question: the 168 settled live bets, resting post-only fills against takes at the ask, by how far under the decision ask a resting bid filled, and by minutes into the window. Net per $1 staked with its standard error, held to the result (contracts - stake on a win, minus stake on a loss). Not covered: queue position, fees on takes.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/multiscale_fill_gap.py
"""

import re, math
from kalshi_moonshot import config
from kalshi_moonshot.store import Store
s = config.reload(); st = Store(s.db_path, s.exclude_versions)
rows = st.db.execute("SELECT d.ts,d.side,d.ask,d.contracts,d.stake_usd,d.outcome,d.reason,w.close_ts FROM decisions d JOIN windows w ON w.ticker=d.ticker WHERE d.action='bet' AND d.lane='crypto_multiscale' AND d.mode='trade' AND d.outcome IN ('yes','no') AND w.close_ts>0 AND d.contracts>0 ORDER BY d.ts").fetchall()
mid = rows[len(rows)//2]["ts"]
data = []
for r in rows:
    m = re.search(r"(resting at|@) ([0-9.]+)", r["reason"] or "")
    if not m: continue
    kind = "resting" if m.group(1) == "resting at" else "taker"
    fill = float(m.group(2)); gap = r["ask"] - fill
    won = r["outcome"] == r["side"]; stk = r["stake_usd"] or 0
    e = r["ts"] - (float(r["close_ts"]) - 900)
    data.append((r, kind, gap, (r["contracts"]-stk) if won else -stk, won, e, fill))
print("multiscale settled bets with a fill price:", len(data))
def ms(xs):
    n = len(xs)
    if n == 0: return "n=0"
    stk = sum(x[0]["stake_usd"] or 0 for x in xs); net = sum(x[3] for x in xs)
    per = [x[3] / max(x[0]["stake_usd"] or 1e-9, 1e-9) for x in xs]
    m = sum(per)/n; sd = (sum((p-m)**2 for p in per)/max(n-1,1))**0.5
    a = sum(x[3] for x in xs if x[0]["ts"] <= mid); h = sum(x[3] for x in xs if x[0]["ts"] > mid)
    w = sum(1 for x in xs if x[4])
    return f"n={n:3d} won {100*w/n:3.0f}% vs fill {100*sum(x[6] for x in xs)/n:3.0f}% | net per $1 {m:+.2f} +/- {sd/math.sqrt(n):.2f} (${net:+.2f}) | halves ${a:+.2f}/${h:+.2f}"
print("\nby how the buy filled:")
for k in ("resting", "taker"):
    print(f"  {k:8s}", ms([x for x in data if x[1] == k]))
print("\nresting fills by how far under the decision ask they filled (cents):")
for lo, hi in ((-1, 0.015), (0.015, 0.035), (0.035, 0.06), (0.06, 1)):
    print(f"  gap {lo*100:.1f}-{hi*100:.1f}c ", ms([x for x in data if x[1] == "resting" and lo <= x[2] < hi]))
print("\nresting vs taker by minutes into the window:")
for lo, hi in ((0, 4), (4, 9), (9, 15)):
    for k in ("resting", "taker"):
        print(f"  {lo}-{hi} min {k:8s}", ms([x for x in data if x[1] == k and lo*60 <= x[5] < hi*60]))
print("DONE")
