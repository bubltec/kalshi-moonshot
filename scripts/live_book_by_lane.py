"""The live book since 10-28: realised net by lane and by market from the decisions table (mode 'trade', one row per market and side with a stake; exits at their exit price, otherwise settlement), fee on both legs, plus net
by day. Shows where the live money was made and lost, not a backtest."""
import time, calendar, collections, statistics as S
from kalshi_moonshot import config
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions)
t0=calendar.timegm((2026,10,28,0,0,0))
rows=st.db.execute("SELECT ts,ticker,lane,side,ask,stake_usd,outcome,exit_price,exit_reason FROM decisions WHERE action='bet' AND mode='trade' AND stake_usd>0 AND ts>? AND (outcome IS NOT NULL OR exit_price IS NOT NULL) ORDER BY ts",(t0,)).fetchall()
seen={}
for r in rows: seen[(r['ticker'],r['lane'],r['side'],round(r['ask'],3))]=r
def pnl(r):
    c=r['stake_usd']/r['ask']
    if r['exit_price'] is not None: return (r['exit_price']-r['ask'])*c-(fee(r['ask'])+fee(max(r['exit_price'],0.01)))*c
    return ((1-r['ask'])*c if r['outcome']==r['side'] else -r['ask']*c)-fee(r['ask'])*c
L=[(r,pnl(r)) for r in seen.values()]
print('live settled bets',len(L),'net $',round(sum(p for _,p in L),2),'staked $',round(sum(r['stake_usd'] for r,_ in L),2))
def tab(name,key):
    d=collections.defaultdict(list)
    for r,p in L: d[key(r)].append((r,p))
    print('\n'+name+'  (n, net $, staked $, return %, win-or-sold-up %)')
    for k,v in sorted(d.items(),key=lambda kv:-sum(p for _,p in kv[1])):
        st_=sum(r['stake_usd'] for r,_ in v)
        print(f"  {str(k):24} {len(v):4d} {sum(p for _,p in v):+9.2f} {st_:9.2f} {100*sum(p for _,p in v)/st_:+6.1f}%  {100*sum(1 for _,p in v if p>0)/len(v):3.0f}%")
tab('by lane',lambda r:r['lane'])
tab('by market',lambda r:r['ticker'].split('-')[0].replace('15M',''))
tab('by day (UTC)',lambda r:time.strftime('%m-%d',time.gmtime(r['ts'])))
ex=collections.Counter((r['exit_reason'] or 'held')[:18] for r,_ in L); print('\nexit reasons:',dict(ex.most_common(8)))
by=collections.defaultdict(float)
for r,p in L: by[(r['exit_reason'] or 'held to result')[:18]]+=p
print('net by exit reason:',{k:round(v,1) for k,v in sorted(by.items(),key=lambda kv:kv[1])})
print('DONE')
