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
med={c:statistics.median(r['v'] for r in recs if r['coin']==c) for c in IDX if any(r['coin']==c for r in recs)}
for r in recs: r['rv']=r['v']/med[r['coin']]
import json, gzip, base64
cols=["ticker","coin","side","ts","close_ts","ask","bid","left_s","won","pnl","flip_pnl","flip_price","z","slope","crosses","vol_ratio","since_cross","variety","v","rv"]
data=[[r.get(c) for c in cols] for r in recs]
b=base64.b64encode(gzip.compress(json.dumps({"cols":cols,"rows":data}).encode())).decode()
open('/tmp/vol.b64','w').write('\n'.join(b[i:i+1000] for i in range(0,len(b),1000))+'\n')
print(len(recs),'rows','lines',len(b)//1000+1)
print('DONE')
