import os, time, bisect, collections, statistics, random
os.nice(15)
from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); now=time.time(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
GROUPS={"treasury":("KX2YRRATE15M","KX5YRRATE15M","KX10YRRATE15M","KX30YRRATE15M"),"crypto":("KXBTC15M","KXETH15M","KXSOL15M","KXDOGE15M"),"commodity":("KXGOLD15M","KXSILVER15M","KXWTI15M","KXBRENT15M","KXCOPPER15M","KXNATGAS15M","KXPLATINUM15M","KXPALLADIUM15M")}
STEP=5.0
def grid(ticker,close):
    q=tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",(ticker,close-930,close)).fetchall()
    if len(q)<40: return None
    ts=[r[0] for r in q]; out=[]
    t=close-900
    while t<=close-45:
        k=bisect.bisect_right(ts,t)-1
        if k>=0 and t-ts[k]<20: out.append((float(q[k][1]),float(q[k][2])))
        else: out.append(None)
        t+=STEP
    return out
def sides(g):
    return [[(b,a) if g[i] else None for i,(b,a) in enumerate([x or (0,0) for x in g])], [((1-a),(1-b)) if g[i] else None for i,(b,a) in enumerate([x or (0,0) for x in g])]]
data={}
random.seed(1)
for name,series in GROUPS.items():
    W=st.db.execute(f"SELECT ticker,series,close_ts FROM windows WHERE series IN ({','.join('?'*len(series))}) AND result IN ('yes','no') AND close_ts>? ORDER BY close_ts",(*series,now-3*86400)).fetchall()
    W=list(W); random.shuffle(W); W=W[:450]
    gs=[g for g in (grid(w['ticker'],float(w['close_ts'])) for w in W) if g]
    data[name]=gs
print({k:len(v) for k,v in data.items()})
# variance ratio of mid changes, mid-band only
for name,gs in data.items():
    d1=[];dk=[]
    for g in gs:
        mids=[(b+a)/2 if (b,a)!=(0,0) and x else None for x in g for (b,a) in [x or (0,0)]]
        for i in range(12,len(mids)-12):
            if mids[i] and mids[i-1] and 0.2<mids[i]<0.8: d1.append(mids[i]-mids[i-1])
            if mids[i] and mids[i-6] and 0.2<mids[i]<0.8: dk.append(mids[i]-mids[i-6])
    if d1 and dk: print(name,'VR(30s/5s)',round(statistics.pvariance(dk)/(6*statistics.pvariance(d1)),2),'sd5s',round(statistics.pstdev(d1),4),'n',len(d1))
def sim(gs,mode,k,dip,stop=0.06,hold=24,wait=4):
    n_att=n_fill=0; res=[]; trips=0
    for g in gs:
        for sd in sides(g):
            i=12
            while i<len(sd)-14:
                x=sd[i]
                if not x or not (0.2<=x[1]<=0.8): i+=1; continue
                if dip>0:
                    prev=[sd[j][1] for j in range(max(0,i-6),i) if sd[j]]
                    if not prev or x[1]>max(prev)-dip: i+=1; continue
                n_att+=1
                if mode=="taker": pa=x[1]; ef=fee(pa); j0=i
                else:
                    pa=x[0]; ef=0.0; filled=None
                    for j in range(i+1,min(i+1+wait,len(sd))):
                        y=sd[j]
                        if y and y[1]<=pa+1e-9: filled=j; break
                    if filled is None: i+=wait; continue
                    j0=filled
                n_fill+=1; exit_px=None; xf=0.0; end=min(j0+hold,len(sd)-1)
                for j in range(j0+1,end+1):
                    y=sd[j]
                    if not y: continue
                    if y[0]>=pa+k: exit_px=pa+k; break
                    if y[0]<=pa-stop: exit_px=y[0]; xf=fee(max(y[0],0.01)); break
                if exit_px is None:
                    y=next((sd[j] for j in range(end,j0,-1) if sd[j]),None)
                    if not y: i=end+1; continue
                    exit_px=y[0]; xf=fee(max(exit_px,0.01))
                res.append(exit_px-pa-ef-xf); trips+=1; i=end+1
    return n_att,n_fill,res,trips
print('group mode k dip | attempts fills trades  mean_c  win%  net_c/window(per side)')
for name,gs in data.items():
    for mode in ("taker","maker"):
        for k in (0.04,0.08):
            for dip in (0.0,0.04):
                a,f,r,t=sim(gs,mode,k,dip)
                if r: print(f"{name:9} {mode:6} {k:.2f} {dip:.2f} | {a:5d} {f:5d} {len(r):5d} {statistics.mean(r)*100:+6.2f} {sum(1 for x in r if x>0)/len(r)*100:4.0f}%  {sum(r)*100/(2*len(gs)):+7.2f}")
print('DONE')
