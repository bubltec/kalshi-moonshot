import os, time, bisect, statistics, collections, math
os.nice(15)
from kalshi_moonshot import config, db
from kalshi_moonshot.variants import favorite_entry
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract
s=config.reload(); now=time.time(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
W=st.db.execute("SELECT ticker,series,close_ts,result FROM windows WHERE series LIKE 'KX%YRRATE15M' AND result IN ('yes','no') ORDER BY close_ts").fetchall()
rows=[]
for w in W:
    close=float(w['close_ts'])
    q=tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",(w['ticker'],close-900,close)).fetchall()
    if len(q)<30: continue
    qq=[(float(a),float(b),float(c)) for a,b,c in q]
    pk=favorite_entry([x for x in qq if close-x[0]<=480 and close-x[0]>=120],close)
    if not pk: continue
    side,ask,at,bid=pk
    ts=[x[0] for x in qq]
    def midat(t):
        k=bisect.bisect_right(ts,t)-1
        return None if k<0 or t-ts[k]>20 else (qq[k][1]+qq[k][2])/2
    grid=[midat(at-300+5*i) for i in range(61)]
    if sum(1 for g in grid if g is not None)<40: continue
    g=[x for x in grid if x is not None]
    cross=sum(1 for a,b in zip(g,g[1:]) if (a-0.5)*(b-0.5)<0)
    sgn=1 if side=='yes' else -1
    mid_now=g[-1]; fav_mid=mid_now if side=='yes' else 1-mid_now
    back=midat(at-120); slope=None if back is None else sgn*(mid_now-back)
    ch=[b-a for a,b in zip(g[-25:],g[-24:])]; vol=statistics.pstdev(ch) if len(ch)>3 else 0
    c=10/ask; won=w['result']==side
    pnl=((1-ask)*c if won else -10)-fee_per_contract(ask)*c
    rows.append(dict(series=w['series'][2:-3],side=side,ask=ask,left=close-at,cross=cross,slope=slope,vol=vol,won=won,pnl=pnl,day=time.strftime('%m-%d',time.gmtime(close-5*3600))))
def show(title,key,order=None):
    g=collections.defaultdict(lambda:[0,0,0.0])
    for r in rows:
        k=key(r)
        if k is None: continue
        g[k][0]+=1; g[k][1]+=r['won']; g[k][2]+=r['pnl']
    print(title)
    for k in (order or sorted(g)):
        if k in g: n,w,p=g[k]; print(f"  {str(k):22} n{n:4d} win{w/n*100:4.0f}% net{p:+7.1f} /bet{p/n:+5.2f}")
n=len(rows); w=sum(r['won'] for r in rows); p=sum(r['pnl'] for r in rows)
avgask=statistics.mean(r['ask'] for r in rows)
print(f"ALL favorites n{n} win{w/n*100:.0f}% net{p:+.1f} avg ask {avgask:.3f} (break-even about {avgask+0.02:.2f})")
lo=w/n-1.96*math.sqrt(w/n*(1-w/n)/n); print(f"  95% interval on win rate: {lo*100:.0f}% to {(2*w/n-lo)*100:.0f}%")
show('by series',lambda r:r['series'])
show('by day',lambda r:r['day'])
show('crossings of 50c in last 5 min',lambda r:'0' if r['cross']==0 else '1' if r['cross']==1 else '2' if r['cross']==2 else '3+',['0','1','2','3+'])
show('price moved toward us (2 min)',lambda r:None if r['slope'] is None else 'toward >3c' if r['slope']>0.03 else 'away >3c' if r['slope']<-0.03 else 'flat',['toward >3c','flat','away >3c'])
show('ask',lambda r:'.78-.82' if r['ask']<.82 else '.82-.87' if r['ask']<.87 else '.87+',['.78-.82','.82-.87','.87+'])
show('seconds left',lambda r:'120-240' if r['left']<240 else '240-360' if r['left']<360 else '360-480',['120-240','240-360','360-480'])
show('side',lambda r:r['side'])
show('screenshot-like: ask .78-.84, 2+ crossings, 300-420 s left',lambda r:'yes' if (r['ask']<=.84 and r['cross']>=2 and 300<=r['left']<=420) else 'other',['yes','other'])
print('DONE')
