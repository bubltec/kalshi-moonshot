import os, time, bisect, math, collections, calendar, statistics
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.kalshiws import index_for_series as crypto_index
s=config.reload(); now=time.time(); tape=db.connect(s.tape_path,"tape",shared=True)
t0=calendar.timegm((2026,10,26,5,0,0))
recs=[r for r in variants.favorites(s,t0,now-600) if r['cls']=='crypto']
print('crypto favorites',len(recs))
IDX={'BTC':'BRTI','ETH':'ETHUSD_RTI','SOL':'SOLUSD_RTI','DOGE':'DOGEUSD_RTI','XRP':'XRPUSD_RTI'}
mins={}
for coin,ix in IDX.items():
    rows=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(ix,t0-8*3600)).fetchall()
    m={}
    for ts,v in rows: m[int(ts//60)]=float(v)   # last tick of each minute
    mins[coin]=m
def vol6h(coin,ts):
    m=mins[coin]; end=int(ts//60); pts=[m.get(k) for k in range(end-360,end+1)]
    rets=[math.log(b/a) for a,b in zip(pts,pts[1:]) if a and b]
    return statistics.pstdev(rets)*math.sqrt(60)*1e4 if len(rets)>200 else None   # hourly vol in bp
for r in recs: r['v']=vol6h(r['coin'],r['ts'])
recs=[r for r in recs if r['v']]
# per-coin normalisation: vol relative to that coin's median over the period
med={c:statistics.median(r['v'] for r in recs if r['coin']==c) for c in IDX if any(r['coin']==c for r in recs)}
for r in recs: r['rv']=r['v']/med[r['coin']]
def et(ts): return time.strftime('%a %m-%d',time.gmtime(ts-5*3600))
byday=collections.defaultdict(list)
for r in recs: byday[et(r['close_ts'])].append(r)
print('day | favorites | win% | net | mean trailing-6h vol vs the coin median')
for d in sorted(byday,key=lambda x:time.mktime(time.strptime('2026-'+x[4:],'%Y-%m-%d'))):
    v=byday[d]; print(f"{d}  n{len(v):3d} win{sum(r['won'] for r in v)/len(v)*100:4.0f}% net{sum(r['pnl'] for r in v):+7.1f}  vol x{statistics.mean(r['rv'] for r in v):.2f}")
srt=sorted(recs,key=lambda r:r['rv']); n=len(srt)
print('favorites by trailing-6h volatility (relative to the coin median), thirds:')
for name,seg in (('calm third',srt[:n//3]),('middle',srt[n//3:2*n//3]),('wild third',srt[2*n//3:])):
    print(f"  {name:11} vol x{statistics.mean(r['rv'] for r in seg):.2f}  n{len(seg):4d} win{sum(r['won'] for r in seg)/len(seg)*100:4.0f}% net{sum(r['pnl'] for r in seg):+7.1f} per bet {sum(r['pnl'] for r in seg)/len(seg):+5.2f}")
print('finer: vol x below 0.7 / 0.7-0.9 / 0.9-1.1 / 1.1-1.4 / above 1.4')
for lo,hi in ((0,0.7),(0.7,0.9),(0.9,1.1),(1.1,1.4),(1.4,99)):
    seg=[r for r in recs if lo<=r['rv']<hi]
    if seg: print(f"  {lo:.1f}-{hi:.1f}  n{len(seg):4d} win{sum(r['won'] for r in seg)/len(seg)*100:4.0f}% net{sum(r['pnl'] for r in seg):+7.1f} per bet {sum(r['pnl'] for r in seg)/len(seg):+5.2f}")
print('DONE')
