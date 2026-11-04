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
import collections, math
def et(ts): return time.strftime('%a %m-%d',time.gmtime(ts-5*3600))
sel=[r for r in recs if r['rv']>=1.3 and r['ask']>=0.82]
print('WILD (vol >= 1.3x) favorites priced 0.82+: ',len(sel),'| favorite win',round(sum(r['won'] for r in sel)/len(sel)*100),'% at mean ask',round(sum(r['ask'] for r in sel)/len(sel),3),'| underdog mean price',round(sum(r['flip_price'] for r in sel)/len(sel),3))
n=len(sel); w=sum(1-r['won'] for r in sel)/n
print(f'  underdog wins {w*100:.1f}% (95% interval {max(0,w-1.96*math.sqrt(w*(1-w)/n))*100:.1f}-{(w+1.96*math.sqrt(w*(1-w)/n))*100:.1f}%) against a price of {sum(r["flip_price"] for r in sel)/n*100:.1f}c; flip net {sum(r["flip_pnl"] for r in sel):+.1f} ({sum(r["flip_pnl"] for r in sel)/n:+.2f}/bet on $10)')
byday=collections.defaultdict(list)
for r in sel: byday[et(r['close_ts'])].append(r)
print('  by day: n, underdog wins, flip net')
for d in sorted(byday,key=lambda x:time.mktime(time.strptime('2026-'+x[4:],'%Y-%m-%d'))):
    v=byday[d]; print(f"   {d}  n{len(v):3d}  underdog won {sum(1-r['won'] for r in v):3d} ({sum(1-r['won'] for r in v)/len(v)*100:4.1f}%)  flip {sum(r['flip_pnl'] for r in v):+7.1f}")
half=sorted(sel,key=lambda r:r['close_ts']); a,b=half[:n//2],half[n//2:]
for name,x in (('first half',a),('second half',b)):
    print(f"  {name}: n{len(x)} underdog won {sum(1-r['won'] for r in x)/len(x)*100:.1f}% flip {sum(r['flip_pnl'] for r in x):+.1f} ({sum(r['flip_pnl'] for r in x)/len(x):+.2f}/bet)")
for coin in ('BTC','ETH','SOL','DOGE','XRP'):
    x=[r for r in sel if r['coin']==coin]
    if len(x)>=15: print(f"  {coin:5} n{len(x):3d} underdog won {sum(1-r['won'] for r in x)/len(x)*100:4.1f}%  flip {sum(r['flip_pnl'] for r in x):+7.1f}")
rest=[r for r in recs if not (r['rv']>=1.3 and r['ask']>=0.82)]
print('  all OTHER favorites flipped (for contrast): n',len(rest),'flip net',round(sum(r['flip_pnl'] for r in rest),1),'(',round(sum(r['flip_pnl'] for r in rest)/len(rest),2),'/bet)')
print('DONE')
