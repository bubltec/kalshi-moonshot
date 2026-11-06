import calendar, math, bisect
from kalshi_moonshot import config, db, kalshiws
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,'tape',shared=True)
t0=calendar.timegm((2026,10,28,0,0,0))
rows=st.db.execute("SELECT ts,ticker,lane,side,ask,stake_usd,outcome,exit_price FROM decisions WHERE action='bet' AND mode='trade' AND stake_usd>0 AND ts>? AND lane LIKE 'crypto_%' AND lane NOT IN ('crypto_moonshot','crypto_wild_fade') AND (outcome IS NOT NULL OR exit_price IS NOT NULL) ORDER BY ts",(t0,)).fetchall()
seen={}
for r in rows: seen[(r['ticker'],r['lane'],r['side'],round(r['ask'],3))]=r
out=[];skip=0
for r in seen.values():
    idx=kalshiws.index_for_series(r['ticker'].split('-')[0])
    if not idx: skip+=1; continue
    ticks=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts",(idx,r['ts']-900,r['ts'])).fetchall()
    if len(ticks)<300: skip+=1; continue
    T=[x['ts'] for x in ticks]; V=[x['value'] for x in ticks]
    d=[V[i+1]-V[i] for i in range(len(V)-1)]
    unit=math.sqrt(sum(x*x for x in d)/len(d))*math.sqrt(60)   # one typical minute, price units
    if unit<=0: skip+=1; continue
    def val(t):
        i=bisect.bisect_right(T,t)-1
        return V[i] if i>=0 else None
    a=val(r['ts']-90); b=val(r['ts']-420)
    if a is None or b is None: skip+=1; continue
    sg=1 if r['side']=='yes' else -1
    trend=sg*(a-b)/unit
    q=tape.execute("SELECT bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0",(r['ticker'],r['ts']-90,r['ts'])).fetchall()
    rng=(max((x['bid']+x['ask'])/2 for x in q)-min((x['bid']+x['ask'])/2 for x in q)) if len(q)>=4 else None
    c=r['stake_usd']/r['ask']
    p=((r['exit_price']-r['ask'])*c-(fee(r['ask'])+fee(max(r['exit_price'],.01)))*c) if r['exit_price'] is not None else (((1-r['ask'])*c if r['outcome']==r['side'] else -r['ask']*c)-fee(r['ask'])*c)
    out.append((rng,trend,p,r['stake_usd'],r['ts']))
print('bets',len(out),'skipped (no spot history)',skip)
def row(name,v):
    if v: print(f"  {name:40} n={len(v):3d} net ${sum(o[2] for o in v):+7.2f} on ${sum(o[3] for o in v):6.0f} {100*sum(o[2] for o in v)/sum(o[3] for o in v):+5.1f}%")
for nm,f in (('all',lambda o:True),('jumpy (>=15c)',lambda o:o[0] is not None and o[0]>=.15),('calm (<15c)',lambda o:o[0] is not None and o[0]<.15)):
    print(nm)
    v=[o for o in out if f(o)]
    row('  trend with us (>+1 typical min)',[o for o in v if o[1]>1]); row('  flat (-1..+1)',[o for o in v if -1<=o[1]<=1]); row('  trend against us (<-1)',[o for o in v if o[1]<-1])
tm=sorted(o[4] for o in out); cut=tm[len(tm)//2]
for nm,f in (('FIRST HALF',lambda o:o[4]<cut),('SECOND HALF',lambda o:o[4]>=cut)):
    print(nm); v=[o for o in out if f(o)]
    row('  trend with us',[o for o in v if o[1]>1]); row('  flat or against',[o for o in v if o[1]<=1])
days={}
for o in out:
    k=(int(o[4]//86400),o[1]>1); days[k]=days.get(k,0)+o[2]
print('by day (UTC), net: trend-with-us | rest')
for k in sorted({k for k,_ in days}): print('  ',k,'%+.1f | %+.1f'%(days.get((k,True),0),days.get((k,False),0)))
print('DONE')
