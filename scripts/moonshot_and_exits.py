"""(1) crypto_moonshot live record by day, coin, price band and stake filled; the mean and standard error per bet. (2) For every live bet that was sold early, what holding it to the result would have made (the outcome of the market is
in the row once it settles): exit net vs hold net by exit reason, so the exits can be judged against their own counterfactual."""
import time, calendar, collections, math, statistics as S
from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); t0=calendar.timegm((2026,10,28,0,0,0))
rows=st.db.execute("SELECT ts,ticker,lane,side,ask,stake_usd,contracts,outcome,exit_price,exit_reason FROM decisions WHERE action='bet' AND mode='trade' AND stake_usd>0 AND ts>? ORDER BY ts",(t0,)).fetchall()
seen={}
for r in rows: seen[(r['ticker'],r['lane'],r['side'],round(r['ask'],3))]=r
def pnl_exit(r):
    c=r['stake_usd']/r['ask']; return (r['exit_price']-r['ask'])*c-(fee(r['ask'])+fee(max(r['exit_price'],0.01)))*c
def pnl_hold(r):
    c=r['stake_usd']/r['ask']; return ((1-r['ask'])*c if r['outcome']==r['side'] else -r['ask']*c)-fee(r['ask'])*c
def pnl(r): return pnl_exit(r) if r['exit_price'] is not None else pnl_hold(r)
M=[r for r in seen.values() if r['lane']=='crypto_moonshot' and (r['outcome'] or r['exit_price'] is not None)]
P=[pnl(r) for r in M]
print(f"crypto_moonshot: {len(M)} bets, net ${sum(P):+.2f}, staked ${sum(r['stake_usd'] for r in M):.2f}, wins {sum(1 for p in P if p>0)}, mean per bet {S.mean(P):+.2f} (standard error {S.pstdev(P)/math.sqrt(len(P)):.2f})")
def tab(name,key):
    d=collections.defaultdict(list)
    for r in M: d[key(r)].append(pnl(r))
    print(name); 
    for k,v in sorted(d.items()): print(f"  {str(k):14} n{len(v):4d} net {sum(v):+8.2f}  wins {sum(1 for x in v if x>0):3d} ({100*sum(1 for x in v if x>0)/len(v):2.0f}%)  mean {S.mean(v):+.2f}")
tab('by day (UTC)',lambda r:time.strftime('%m-%d',time.gmtime(r['ts'])))
tab('by coin',lambda r:r['ticker'].split('-')[0].replace('15M',''))
tab('by price band',lambda r:'<5c' if r['ask']<0.05 else '5-10c' if r['ask']<0.10 else '10-15c' if r['ask']<0.15 else '15c+')
tab('by side',lambda r:r['side'])
print('\nSOLD EARLY: exit net vs holding to the result (rows with an outcome)')
by=collections.defaultdict(lambda:[0,0.0,0.0])
for r in seen.values():
    if r['exit_price'] is None or not r['outcome']: continue
    k=(r['exit_reason'] or '?')[:16]; v=by[k]; v[0]+=1; v[1]+=pnl_exit(r); v[2]+=pnl_hold(r)
for k,(n,e,h) in sorted(by.items(),key=lambda kv:-kv[1][0])[:8]: print(f"  {k:18} n{n:4d}  sold {e:+9.2f}  would-have-held {h:+9.2f}  difference {e-h:+8.2f}")
print('DONE')
