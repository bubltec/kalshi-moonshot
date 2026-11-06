"""Return by the price paid (user: lower prices should lose more, so the stake should scale with price). Every settled live bet since 10-28 (one per market, side and lane), by lane group and price bucket: bets, win rate, net per $ staked after fees, total losses."""
import calendar, collections
from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions)
t0=calendar.timegm((2026,10,28,0,0,0))
rows=st.db.execute("SELECT ts,ticker,lane,side,ask,stake_usd,outcome,exit_price FROM decisions WHERE action='bet' AND mode='trade' AND stake_usd>0 AND ts>? AND (outcome IS NOT NULL OR exit_price IS NOT NULL) ORDER BY ts",(t0,)).fetchall()
seen={}
for r in rows: seen[(r['ticker'],r['lane'],r['side'],round(r['ask'],3))]=r
def pnl(r):
    c=r['stake_usd']/r['ask']
    if r['exit_price'] is not None: return (r['exit_price']-r['ask'])*c-(fee(r['ask'])+fee(max(r['exit_price'],.01)))*c
    return ((1-r['ask'])*c if r['outcome']==r['side'] else -r['ask']*c)-fee(r['ask'])*c
def grp(l):
    if l.startswith('commodity') : return 'commodity'
    if l.startswith('crypto') and l not in ('crypto_moonshot','crypto_wild_fade'): return 'crypto favorites'
    return None
B=[(0,0.30),(0.30,0.50),(0.50,0.65),(0.65,0.75),(0.75,0.82),(0.82,0.88),(0.88,0.93),(0.93,1.01)]
d=collections.defaultdict(list)
for r in seen.values():
    g=grp(r['lane'])
    if not g or r['ask']<=0: continue
    for lo,hi in B:
        if lo<=r['ask']<hi: d[(g,lo,hi)].append(r); break
for g in ('commodity','crypto favorites'):
    print(g)
    for lo,hi in B:
        v=d.get((g,lo,hi),[])
        if len(v)<5: continue
        ps=[pnl(r) for r in v]; stk=sum(r['stake_usd'] for r in v)
        print(f"  ask {lo:.2f}-{hi:.2f}: n={len(v):4d} win {100*sum(1 for p in ps if p>0)/len(v):3.0f}%  net ${sum(ps):+8.2f} on ${stk:7.0f} {100*sum(ps)/stk:+6.1f}%  total losses {sum(1 for r in v if r['exit_price'] is None and r['outcome']!=r['side'])}")
print('DONE')
