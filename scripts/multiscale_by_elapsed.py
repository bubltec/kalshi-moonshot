"""Multiscale bets by how early in the 15 minute window they were bought (2026-11-08, user: "the early buys make me weary, but do they actually win?").

Question: do the crypto_multiscale live bets bought early in the window win less than late ones? Settled live bets held to the result, net in dollars (contracts - stake on a win, minus stake on a loss; resting fills pay no fee), by minutes into the window, halves by bet time. Cutoffs are a grid, not chosen. Not covered: sales before the result.

    scripts/hostrun3.sh-style: docker compose exec -T bot python - < scripts/multiscale_by_elapsed.py
"""

from kalshi_moonshot import config
from kalshi_moonshot.store import Store
s = config.reload(); st = Store(s.db_path, s.exclude_versions)
cl = {r["ticker"]: float(r["close_ts"]) for r in st.db.execute("SELECT ticker,close_ts FROM windows WHERE close_ts>1793000000").fetchall()}
rows = st.db.execute("SELECT ts,ticker,side,ask,contracts,stake_usd,outcome FROM decisions WHERE action='bet' AND lane='crypto_multiscale' AND mode='trade' AND outcome IN ('yes','no') ORDER BY ts").fetchall()
mid = rows[len(rows)//2]["ts"]
data=[]
for r in rows:
    c = cl.get(r["ticker"])
    if not c: continue
    e = r["ts"] - (c-900)
    won = r["outcome"]==r["side"]; stk=r["stake_usd"] or 0
    data.append((e, r["ask"], won, (r["contracts"]-stk) if won else -stk, stk, r["ts"]))
print("bets", len(data))
def band(lab, rs):
    if not rs: print(f"  {lab:20s} n=0"); return
    n=len(rs); w=sum(1 for x in rs if x[2]); net=sum(x[3] for x in rs); stk=sum(x[4] for x in rs)
    a=sum(x[3] for x in rs if x[5]<=mid); b=sum(x[3] for x in rs if x[5]>mid)
    print(f"  {lab:20s} n={n:3d} won {w:3d} ({100*w/n:3.0f}%) avg ask {sum(x[1] for x in rs)/n:.2f} stake ${stk:6.2f} net ${net:+6.2f} ({100*net/max(stk,1e-9):+5.0f}%) halves ${a:+.2f}/${b:+.2f}")
print("by minutes into the window at the buy")
for lo,hi in ((0,2),(2,4),(4,6),(6,8),(8,11),(11,15)):
    band(f"{lo}-{hi} min", [x for x in data if lo*60<=x[0]<hi*60])
print("early (<4 min) by ask")
for lo,hi in ((0,0.35),(0.35,0.7)):
    band(f"<4min ask {lo}-{hi}", [x for x in data if x[0]<240 and lo<=x[1]<hi])
print("DONE")
