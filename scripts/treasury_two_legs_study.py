import os, time, bisect, statistics, collections
os.nice(15)
from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
s=config.reload(); now=time.time(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
SER=("KX2YRRATE15M","KX5YRRATE15M","KX10YRRATE15M","KX30YRRATE15M")
W=st.db.execute("SELECT ticker,series,close_ts FROM windows WHERE series IN (?,?,?,?) AND result IN ('yes','no') AND close_ts>? ORDER BY close_ts",(*SER,now-3*86400)).fetchall()
byev=collections.defaultdict(dict)
for w in W: byev[round(float(w['close_ts']))][w['series']]=w['ticker']
STEP=5.0
def grid(ticker,close):
    q=tape.execute("SELECT ts,bid,ask,bid_size,ask_size FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",(ticker,close-930,close)).fetchall()
    if len(q)<40: return None
    ts=[r[0] for r in q]; fl=tape.execute("SELECT ts,yes_ct,no_ct FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ?",(ticker,close-930,close)).fetchall() if False else None
    out=[]; t=close-900
    while t<=close-45:
        k=bisect.bisect_right(ts,t)-1
        if k>=0 and t-ts[k]<20: r=q[k]; out.append((float(r[1]),float(r[2]),float(r[3] or 0),float(r[4] or 0)))
        else: out.append(None)
        t+=STEP
    return out
def flowgrid(ticker,close):
    cols=[r[1] for r in tape.execute("PRAGMA table_info(flow_1s)")] if not db.is_pg(tape) else None
    return None
from kalshi_moonshot.strategy import fee_per_contract as fee
H=6
def flows(ticker,close):
    d={}
    for ts,yv,nv,vw in tape.execute("SELECT ts,yes_vol,no_vol,vwap FROM flow_1s WHERE ticker=? AND ts BETWEEN ? AND ? AND vwap IS NOT NULL",(ticker,int(close-930),int(close))):
        d[int(ts)]=(float(yv),float(nv),float(vw))
    return d
def orient(g,yes): return [None if x is None else ((x[0],x[1]) if yes else (1-x[1],1-x[0])) for x in g]
def printfill(fl,yes,t0,t1,price,buy):
    """A taker print that would have filled a resting order of ours at `price` (our own-side price) between t0 and t1 (epoch seconds)."""
    for sec in range(int(t0),int(t1)+1):
        f=fl.get(sec)
        if not f: continue
        yv,nv,vw=f
        own_px=vw if yes else 1-vw
        if buy:   # our bid is hit by a taker selling our side: YES bid hit by taker 'no' buyers, NO bid hit by taker 'yes' buyers
            if (nv if yes else yv)>0 and own_px<=price+1e-9: return True
        else:     # our resting sell is lifted by a taker buying our side
            if (yv if yes else nv)>0 and own_px>=price-1e-9: return True
    return False
def run(mode,k,model,first=60,last=480,wait=4,hold=18,stop=0.08,dipc=0.06,lead=0.05):
    stats=collections.Counter(); res=[]; wins=0; windows=0
    for close,d in byev.items():
        if len(d)<4: continue
        gs={sr:grid(tk,float(close)) for sr,tk in d.items()}
        if any(g is None for g in gs.values()): continue
        fls={sr:flows(tk,float(close)) for sr,tk in d.items()}
        windows+=1; n=min(len(g) for g in gs.values()); base=float(close)-900
        for sr,g in gs.items():
            others=[o for kk,o in gs.items() if kk!=sr]
            for yes in (True,False):
                sd=orient(g,yes); osd=[orient(o,yes) for o in others]; fl=fls[sr]
                i=H
                while i<n-hold-wait-2:
                    tsec=i*STEP
                    x=sd[i]; p=sd[i-H]
                    if tsec<first or tsec>last or not x or not p or not (0.2<=x[1]<=0.8): i+=1; continue
                    mid=lambda z:(z[0]+z[1])/2
                    own=mid(x)-mid(p)
                    oth=[mid(o[i])-mid(o[i-H]) for o in osd if o[i] and o[i-H]]
                    if len(oth)<3: i+=1; continue
                    om=statistics.mean(oth); dip=own<=-dipc; ld=(om>=lead and own<=0)
                    if not {"dip":dip,"lead":ld,"either":dip or ld}[mode]: i+=1; continue
                    pa=x[0]; stats['placed']+=1; j0=None
                    for j in range(i+1,i+1+wait):
                        y=sd[j]
                        if model=="quote":
                            if y and y[1]<=pa+1e-9: j0=j; break
                        else:
                            if printfill(fl,yes,base+(j-1)*STEP,base+j*STEP,pa,True) or (y and y[1]<=pa+1e-9): j0=j; break
                    if j0 is None: i+=wait; continue
                    stats['filled']+=1; target=pa+k; out=None; end=min(j0+hold,n-1)
                    for j in range(j0+1,end+1):
                        y=sd[j]
                        if not y: continue
                        hit=(y[0]>=target) if model=="quote" else (y[0]>=target or printfill(fl,yes,base+(j-1)*STEP,base+j*STEP,target,False))
                        if hit: out=k; stats['target']+=1; break
                        if y[0]<=pa-stop: out=y[0]-pa-fee(max(y[0],0.01)); stats['stop']+=1; break
                    if out is None:
                        y=next((sd[j] for j in range(end,j0,-1) if sd[j]),None)
                        if not y: i=end+1; continue
                        out=y[0]-pa-fee(max(y[0],0.01)); stats['timeout']+=1
                    res.append(out); wins+=out>0; i=end+1
    return res,stats,windows
print('fill model | mode k | placed filled% trades mean_c win% | target% stop% timeout% | net_c/window')
for model in ("quote","print"):
    for mode in ("dip","lead","either"):
        for k in (0.03,0.05,0.08):
            r,s_,w=run(mode,k,model)
            if r:
                n=len(r); print(f"{model:5} | {mode:6} {k:.2f} | {s_['placed']:5d} {s_['filled']/s_['placed']*100:4.0f}% {n:4d} {statistics.mean(r)*100:+6.2f} {sum(1 for x in r if x>0)/n*100:4.0f}% | {s_['target']/n*100:3.0f}% {s_['stop']/n*100:3.0f}% {s_['timeout']/n*100:3.0f}% | {sum(r)*100/w:+7.2f}")
print('DONE')
