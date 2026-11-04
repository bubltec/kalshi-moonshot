import time, calendar, collections, bisect, statistics, re
from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
t0=calendar.timegm((2026,11,2,5,0,0))
rows=st.db.execute("SELECT id,ts,lane,ticker,side,ask,stake_usd,mode,reason FROM decisions WHERE action='bet' AND ts>? AND (mode='trade' OR reason LIKE '%live order not filled%') ORDER BY ts",(t0,)).fetchall()
orders=collections.defaultdict(list)
for o in st.db.execute("SELECT ticker,ts,fill_count,cost_usd,fee_usd,price,COALESCE(side,'buy') sd FROM orders WHERE ts>? AND COALESCE(side,'buy')='buy'",(t0,)).fetchall(): orders[o['ticker']].append(o)
ALLOW={'crypto_moonshot':0.01,'energy_moonshot':0.20}
cases=[]
for r in rows:
    live=r['mode']=='trade'
    os_=[o for o in orders.get(r['ticker'],[]) if -2<=o['ts']-r['ts']<=60 and (o['fill_count'] or 0)>0]
    actual_fill=None; lat=None
    if live and os_:
        o=os_[0]; n=sum(x['fill_count'] for x in os_); cost=sum(x['cost_usd'] for x in os_); fee=sum((x['fee_usd'] or 0) for x in os_)
        actual_fill=(cost-fee)/n; lat=o['ts']-r['ts']
    q=tape.execute("SELECT ts,bid,ask,bid_size,ask_size FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",(r['ticker'],r['ts']-15,r['ts']+30)).fetchall()
    if len(q)<3: continue
    cases.append((r,live,actual_fill,lat,q))
print('live attempts with tape quotes:',len(cases),'| filled live:',sum(1 for c in cases if c[1]),'| missed:',sum(1 for c in cases if not c[1]))
lats=[c[3] for c in cases if c[3] is not None]
print('order timestamp minus decision timestamp, filled live (s): median',round(statistics.median(lats),2),'p10',round(sorted(lats)[len(lats)//10],2),'p90',round(sorted(lats)[9*len(lats)//10],2),'n',len(lats))
def side_ask(row,side): return float(row[2]) if side=='yes' else 1-float(row[1])
def predict(c,L,allow,size_rule):
    r,live,af,lat,q=c; ts=[x[0] for x in q]
    k=bisect.bisect_left(ts,r['ts']+L)
    if k>=len(q): k=len(q)-1
    a=side_ask(q[k],r['side']); lim=r['ask']+allow
    return (a<=lim+1e-9), a
print('lane | L | n | classify correct% (filled/missed) | mean abs price error on fills (c) | model fill rate vs actual fill rate')
for lane in sorted({c[0]['lane'] for c in cases}):
    cs=[c for c in cases if c[0]['lane']==lane]
    if len(cs)<10: continue
    allow=ALLOW.get(lane,0.03)
    actual_rate=sum(1 for c in cs if c[1])/len(cs)
    for L in (0.0,0.5,1.0,2.0,4.0):
        ok=0; errs=[]; mrate=0
        for c in cs:
            f,a=predict(c,L,allow,None); mrate+=f
            ok+= (f==c[1])
            if c[1] and c[2] is not None: errs.append(abs(a-c[2]))
        print(f"  {lane:20} L{L:3.1f} n{len(cs):4d}  correct {ok/len(cs)*100:3.0f}%  price err {statistics.mean(errs)*100 if errs else float('nan'):5.2f}c  model fills {mrate/len(cs)*100:3.0f}% actual {actual_rate*100:3.0f}%")
print('DONE')
