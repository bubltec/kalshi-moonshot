import os, time, math, statistics, collections, calendar
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.biquotefeed import index_for_series as bq
s=config.reload(); now=time.time(); tape=db.connect(s.tape_path,"tape",shared=True)
t0=calendar.timegm((2026,11,2,14,0,0))   # BiQuote feed began 11-02 13:45 UTC
recs=[r for r in variants.favorites(s,t0,now-600) if r['cls']=='commodity']
print('commodity favorites',len(recs))
mins={}
for coin in {r['series'] for r in recs}:
    ix=bq(coin)
    m={}
    for ts,v in tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(ix,t0-4*3600)): m[int(ts//60)]=float(v)
    mins[coin]=m
def volh(series,ts,look=240):   # trailing 4 h (the feed only goes back about 2.5 days)
    m=mins[series]; end=int(ts//60); pts=[m.get(k) for k in range(end-look,end+1)]
    r=[math.log(b/a) for a,b in zip(pts,pts[1:]) if a and b and abs(math.log(b/a))<0.05]
    return statistics.pstdev(r)*math.sqrt(60)*1e4 if len(r)>120 else None
for r in recs: r['v']=volh(r['series'],r['ts'])
recs=[r for r in recs if r['v']]
med={c:statistics.median(r['v'] for r in recs if r['series']==c) for c in mins}
for r in recs: r['rv']=r['v']/med[r['series']]
print('with a regime reading',len(recs))
srt=sorted(recs,key=lambda r:r['rv']); n=len(srt)
print('commodity favorites by trailing-4h volatility vs the market\'s own median (thirds):')
for name,seg in (('calm third',srt[:n//3]),('middle',srt[n//3:2*n//3]),('wild third',srt[2*n//3:])):
    print(f"  {name:11} vol x{statistics.mean(r['rv'] for r in seg):.2f} n{len(seg):4d} win{sum(r['won'] for r in seg)/len(seg)*100:4.0f}% net{sum(r['pnl'] for r in seg):+7.1f} per bet {sum(r['pnl'] for r in seg)/len(seg):+5.2f}")
print('flip (buy the other side) by regime, favorites priced 0.82+:')
for name,seg in (('calm <=0.8',[r for r in recs if r['rv']<=0.8 and r['ask']>=0.82]),('mid',[r for r in recs if 0.8<r['rv']<1.3 and r['ask']>=0.82]),('wild >=1.3',[r for r in recs if r['rv']>=1.3 and r['ask']>=0.82])):
    if seg: print(f"  {name:11} n{len(seg):4d} favorite won {sum(r['won'] for r in seg)/len(seg)*100:4.0f}% at {sum(r['ask'] for r in seg)/len(seg)*100:.0f}c  favorite net {sum(r['pnl'] for r in seg):+7.1f}  underdog net {sum(r['flip_pnl'] for r in seg):+7.1f}")
by=collections.defaultdict(list)
for r in recs: by[r['coin']].append(r)
print('by metal/energy, wild (>=1.3) vs rest:')
for c,v in sorted(by.items()):
    w=[r for r in v if r['rv']>=1.3]; o=[r for r in v if r['rv']<1.3]
    print(f"  {c:10} wild n{len(w):3d} win {sum(r['won'] for r in w)/max(len(w),1)*100:3.0f}% net {sum(r['pnl'] for r in w):+6.1f} | rest n{len(o):3d} win {sum(r['won'] for r in o)/max(len(o),1)*100:3.0f}% net {sum(r['pnl'] for r in o):+6.1f}")
def et(ts): return time.strftime('%a %m-%d',time.gmtime(ts-5*3600))
byd=collections.defaultdict(list)
for r in recs: byd[et(r['close_ts'])].append(r)
print('by day: n win% net vol x')
for d,v in sorted(byd.items()): print(f"  {d} n{len(v):4d} win{sum(r['won'] for r in v)/len(v)*100:4.0f}% net{sum(r['pnl'] for r in v):+7.1f} vol x{statistics.mean(r['rv'] for r in v):.2f}")
print('DONE')
