import os, time, bisect, math, collections, statistics, random, calendar
os.nice(15)
from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); now=time.time(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
t0=calendar.timegm((2026,10,27,5,0,0))
IDX={'KXBTC15M':'BRTI','KXETH15M':'ETHUSD_RTI','KXSOL15M':'SOLUSD_RTI','KXDOGE15M':'DOGEUSD_RTI'}
mins={}
for ser,ix in IDX.items():
    m={}
    for ts,v in tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(ix,t0-8*3600)): m[int(ts//60)]=float(v)
    mins[ser]=m
def vol6h(ser,ts):
    m=mins[ser]; end=int(ts//60); pts=[m.get(k) for k in range(end-360,end+1)]
    r=[math.log(b/a) for a,b in zip(pts,pts[1:]) if a and b]
    return statistics.pstdev(r)*math.sqrt(60)*1e4 if len(r)>200 else None
W=st.db.execute("SELECT ticker,series,close_ts FROM windows WHERE series IN ('KXBTC15M','KXETH15M','KXSOL15M','KXDOGE15M') AND result IN ('yes','no') AND close_ts>? ORDER BY close_ts",(t0+86400,)).fetchall()
random.seed(3); W=list(W); random.shuffle(W); W=W[:900]
STEP=5.0
def grid(ticker,close):
    q=tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",(ticker,close-930,close)).fetchall()
    if len(q)<40: return None
    ts=[r[0] for r in q]; out=[]; t=close-900
    while t<=close-45:
        k=bisect.bisect_right(ts,t)-1
        out.append((float(q[k][1]),float(q[k][2])) if k>=0 and t-ts[k]<20 else None); t+=STEP
    return out
items=[]
for w in W:
    c=float(w['close_ts']); v=vol6h(w['series'],c-900)
    g=grid(w['ticker'],c)
    if v and g: items.append((w['series'],v,g))
med={ser:statistics.median(v for s_,v,g in items if s_==ser) for ser in IDX}
data={'wild':[],'mid':[],'calm':[]}
for ser,v,g in items:
    rv=v/med[ser]; data['wild' if rv>=1.3 else 'calm' if rv<=0.8 else 'mid'].append(g)
print({k:len(v) for k,v in data.items()})
def sides(g): return [g,[None if x is None else (1-x[1],1-x[0]) for x in g]]
def sim(gs,mode,k,dip,stop=0.08,hold=18,wait=4,first=60,last=600):
    res=[];wins=0
    for g in gs:
        for sd in sides(g):
            i=12
            while i<len(sd)-hold-wait-2:
                x=sd[i]; ts_=i*STEP
                if not x or not (0.2<=x[1]<=0.8) or ts_<first or ts_>last: i+=1; continue
                if dip>0:
                    prev=[sd[j][1] for j in range(max(0,i-6),i) if sd[j]]
                    if not prev or x[1]>max(prev)-dip: i+=1; continue
                if mode=='taker': pa=x[1]; ef=fee(pa); j0=i
                else:
                    pa=x[0]; ef=0.0; j0=None
                    for j in range(i+1,i+1+wait):
                        y=sd[j]
                        if y and y[1]<=pa+1e-9: j0=j; break
                    if j0 is None: i+=wait; continue
                out=None; end=min(j0+hold,len(sd)-1)
                for j in range(j0+1,end+1):
                    y=sd[j]
                    if not y: continue
                    if y[0]>=pa+k: out=k-ef; break
                    if y[0]<=pa-stop: out=y[0]-pa-ef-fee(max(y[0],.01)); break
                if out is None:
                    y=next((sd[j] for j in range(end,j0,-1) if sd[j]),None)
                    if not y: i=end+1; continue
                    out=y[0]-pa-ef-fee(max(y[0],.01))
                res.append(out); wins+=out>0; i=end+1
    return res
print('regime mode k dip | trades mean_c win% net_c per window(both sides)')
for reg,gs in data.items():
    for mode in ('taker','maker'):
        for k in (0.04,0.08):
            for dip in (0.0,0.05):
                r=sim(gs,mode,k,dip)
                if r: print(f"{reg:5} {mode:6} {k:.2f} {dip:.2f} | {len(r):5d} {statistics.mean(r)*100:+6.2f} {sum(1 for x in r if x>0)/len(r)*100:4.0f}% {sum(r)*100/len(gs):+7.2f}")
print('DONE')
