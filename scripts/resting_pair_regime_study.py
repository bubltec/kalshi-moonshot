import os, time, bisect, math, collections, statistics, random, calendar
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
s=config.reload(); now=time.time(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
t0=calendar.timegm((2026,10,27,5,0,0))
IDX={'KXBTC15M':'BRTI','KXETH15M':'ETHUSD_RTI','KXSOL15M':'SOLUSD_RTI','KXDOGE15M':'DOGEUSD_RTI'}
cacheT={};mins={}
def ticks(ix):
    if ix not in cacheT:
        r=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(ix,t0-8*3600)).fetchall(); cacheT[ix]=([x[0] for x in r],[x[1] for x in r])
        mins[ix]={int(a//60):b for a,b in zip(*cacheT[ix])}
    return cacheT[ix]
def vol6h(ix,ts):
    m=mins[ix]; end=int(ts//60); pts=[m.get(k) for k in range(end-360,end+1)]
    r=[math.log(b/a) for a,b in zip(pts,pts[1:]) if a and b]
    return statistics.pstdev(r)*math.sqrt(60)*1e4 if len(r)>200 else None
import sys
LO,TGT=0.40,0.80
W=st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE series IN ('KXBTC15M','KXETH15M','KXSOL15M','KXDOGE15M') AND result IN ('yes','no') AND strike>0 AND close_ts>? ORDER BY close_ts",(t0+86400,)).fetchall()
random.seed(9); W=list(W); random.shuffle(W); W=W[:1400]
recs=[]
for w in W:
    ix=IDX[w['series']]; ts_,vs=ticks(ix); close=float(w['close_ts']); K=float(w['strike'])
    q=tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",(w['ticker'],close-905,close)).fetchall()
    if len(q)<40: continue
    v=vol6h(ix,close-900)
    for side in ('yes','no'):
        placed=False
        for idx,(t_,b,a) in enumerate(q):
            left=close-t_
            if left>780 or left<120: continue
            sb,sa=(float(b),float(a)) if side=='yes' else (1-float(a),1-float(b))
            if not placed:
                if sa>LO+0.005: placed=True
                continue
            if sa>LO: continue
            snap=variants.snapshot(ts_,vs,K,side,t_,close)
            hit=False
            for (t2,b2,a2) in q[idx+1:]:
                bb=float(b2) if side=='yes' else 1-float(a2)
                if bb>=TGT: hit=True; break
            won=w['result']==side
            pnl=(TGT-LO) if hit else ((1-LO) if won else -LO)   # both legs resting: no fee
            recs.append(dict(series=w['series'],v=v,variety=(snap['variety'] if snap else '?'),left=left,hit=hit,won=won,pnl=pnl,z=(snap['z'] if snap else 0)))
            break
print('resting bid at 40c, sell at 80c, filled in',len(recs),'cases; reached 60c',round(sum(r['hit'] for r in recs)/len(recs)*100),'%; mean pnl',round(statistics.mean(r['pnl'] for r in recs)*100,2),'c per contract (fair = 0 less adverse selection)')
med={}
for ser in IDX:
    vv=[r['v'] for r in recs if r['series']==ser and r['v']]; med[ser]=statistics.median(vv) if vv else 1
for r in recs: r['rv']=(r['v']/med[r['series']]) if r['v'] else None
def show(title,key,order):
    g=collections.defaultdict(list)
    for r in recs:
        k=key(r)
        if k is not None: g[k].append(r)
    print(title)
    for k in order:
        v=g.get(k)
        if v and len(v)>=25:
            n=len(v); m=statistics.mean(x['pnl'] for x in v); se=statistics.pstdev([x['pnl'] for x in v])/math.sqrt(n)
            print(f"   {k:12} n{n:4d}  hit60 {sum(x['hit'] for x in v)/n*100:3.0f}%  settle-win {sum(x['won'] for x in v)/n*100:3.0f}%  mean {m*100:+6.2f}c ± {se*100:.1f}")
show('by volatility regime',lambda r:None if r['rv'] is None else 'wild>=1.3' if r['rv']>=1.3 else 'calm<=0.8' if r['rv']<=0.8 else 'mid',['wild>=1.3','mid','calm<=0.8'])
show('by variety of that side',lambda r:r['variety'],list('ABCDEFG?'))
show('by time left at the fill',lambda r:'120-300' if r['left']<300 else '300-540' if r['left']<540 else '540-780',['120-300','300-540','540-780'])
show('wild x variety',lambda r:None if r['rv'] is None or r['rv']<1.3 else r['variety'],list('ABCDEFG?'))
show('calm x variety',lambda r:None if r['rv'] is None or r['rv']>0.8 else r['variety'],list('ABCDEFG?'))
print('DONE')
