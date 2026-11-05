"""Does a turn toward the underdog just before the buy pick the wild-regime underdogs that win? Same setups as crypto_wild_fade_study (wild vol, favorite 0.82+), one row per window, then the
second-by-second index path in the 5 minutes before the buy cut into a wave feature: d30 (last 30 s toward the underdog), prior (the 90 s before that, positive = already toward), and rise (how far
the underdog's cushion has come up from its 4-minute low). All in typical-minute units. Win rate, flip net per $10 bet, and both halves for each bucket."""
import os, time, bisect, math, calendar, statistics
os.nice(15)
from kalshi_moonshot import config, db, variants
s=config.reload(); now=time.time(); tape=db.connect(s.tape_path,"tape",shared=True)
t0=calendar.timegm((2026,10,26,5,0,0))
recs=[r for r in variants.favorites(s,t0,now-600) if r['cls']=='crypto']
IDX={'BTC':'BRTI','ETH':'ETHUSD_RTI','SOL':'SOLUSD_RTI','DOGE':'DOGEUSD_RTI','XRP':'XRPUSD_RTI'}
ser={}; mins={}
for coin,ix in IDX.items():
    rows=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(ix,t0-8*3600)).fetchall()
    ser[coin]=([float(a) for a,_ in rows],[float(b) for _,b in rows]); m={}
    for a,b in rows: m[int(a//60)]=float(b)
    mins[coin]=m
def vol6h(coin,ts):
    m=mins[coin]; end=int(ts//60); pts=[m.get(k) for k in range(end-360,end+1)]
    rets=[math.log(b/a) for a,b in zip(pts,pts[1:]) if a and b]
    return statistics.pstdev(rets)*math.sqrt(60)*1e4 if len(rets)>200 else None
for r in recs: r['v']=vol6h(r['coin'],r['ts'])
recs=[r for r in recs if r['v']]
med={c:statistics.median(r['v'] for r in recs if r['coin']==c) for c in IDX if any(r['coin']==c for r in recs)}
sel=[r for r in recs if r['v']/med[r['coin']]>=1.3 and r['ask']>=0.82]
def at(ts,vs,t):
    i=bisect.bisect_right(ts,t)-1
    return vs[i] if i>=0 and t-ts[i]<20 else None
rows=[]
for r in sel:
    ts,vs=ser[r['coin']]; t=r['ts']; u=-1.0 if r['side']=='yes' else 1.0   # underdog direction of price
    path=[(k,at(ts,vs,t-k)) for k in range(0,301,5)]
    if any(p is None for _,p in path): continue
    segs=[math.log(path[i][1]/path[i+1][1])**2 for i in range(len(path)-1)]   # 5 s squared returns, newest first
    unit=math.sqrt(sum(segs)/ (5*len(segs)))*math.sqrt(60)
    if unit<=0: continue
    P=dict(path); mv=lambda a,b: u*math.log(P[a]/P[b])/unit
    d30=mv(0,30); prior=mv(30,120)
    cush=[u*math.log(P[k]/P[300]) for k in range(0,241,5)]   # underdog-side price level (rising = toward underdog) over the last 4 min
    rise=(cush[0]-min(cush))/unit
    rows.append(dict(r=r,d30=d30,prior=prior,rise=rise,uw=1-r['won'],pnl=r['flip_pnl'],close=r['close_ts']))
print('wild setups',len(sel),'with a full 5-min path',len(rows))
def show(name,sub):
    n=len(sub)
    if n<8: print(f'  {name:42} n{n}'); return
    w=sum(x['uw'] for x in sub)/n; h=sorted(sub,key=lambda x:x['close']); a,b=h[:n//2],h[n//2:]
    f=lambda z: f"{sum(x['uw'] for x in z)/len(z)*100:4.1f}%/{sum(x['pnl'] for x in z)/len(z):+.2f}"
    print(f"  {name:42} n{n:4d} won {w*100:4.1f}%  flip {sum(x['pnl'] for x in sub)/n:+6.2f}/bet | halves {f(a)}  {f(b)}")
show('all wild',rows)
for lo,hi in ((-9,-0.5),(-0.5,0),(0,0.5),(0.5,1),(1,9)): show(f'd30 in [{lo},{hi})',[x for x in rows if lo<=x['d30']<hi])
for lo,hi in ((-9,-0.5),(-0.5,0.5),(0.5,9)): show(f'prior in [{lo},{hi})',[x for x in rows if lo<=x['prior']<hi])
show('turn: prior<-0.3 and d30>0.3',[x for x in rows if x['prior']<-0.3 and x['d30']>0.3])
show('turn strong: prior<-0.5 and d30>0.7',[x for x in rows if x['prior']<-0.5 and x['d30']>0.7])
show('already moving toward: prior>0.3 and d30>0.3',[x for x in rows if x['prior']>0.3 and x['d30']>0.3])
for lo,hi in ((0,0.5),(0.5,1),(1,2),(2,99)): show(f'rise from 4-min low in [{lo},{hi})',[x for x in rows if lo<=x['rise']<hi])
print('DONE')
