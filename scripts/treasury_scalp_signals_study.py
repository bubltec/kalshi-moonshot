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
def orient(g,yes):
    return [None if x is None else ((x[0],x[1]) if yes else (1-x[1],1-x[0])) for x in g]
def run(entry_mode,k,maker,hold=18,dipc=0.06,lead=0.05):
    res=[];locks=0;wins=0;windows=0
    for close,d in byev.items():
        if len(d)<4: continue
        gs={sr:grid(tk,float(close)) for sr,tk in d.items()}
        if any(g is None for g in gs.values()): continue
        windows+=1; n=min(len(g) for g in gs.values())
        for sr,g in gs.items():
            others=[o for kk,o in gs.items() if kk!=sr]
            for yes in (True,False):
                sd=orient(g,yes); osd=[orient(o,yes) for o in others]
                i=H
                while i<n-hold-2:
                    x=sd[i]; p=sd[i-H]
                    if not x or not p or not (0.2<=x[1]<=0.8): i+=1; continue
                    mid=lambda z:(z[0]+z[1])/2
                    own=mid(x)-mid(p)
                    oth=[mid(o[i])-mid(o[i-H]) for o in osd if o[i] and o[i-H]]
                    if len(oth)<3: i+=1; continue
                    om=statistics.mean(oth)
                    dip=own<=-dipc; ld=(om>=lead and own<=0)
                    ok={"dip":dip,"lead":ld,"either":dip or ld}[entry_mode]
                    if not ok: i+=1; continue
                    if maker:
                        pa=x[0]; ef=0.0; j0=None
                        for j in range(i+1,i+5):
                            y=sd[j]
                            if y and y[1]<=pa+1e-9: j0=j; break
                        if j0 is None: i+=4; continue
                    else: pa=x[1]; ef=fee(pa); j0=i
                    out=None; end=min(j0+hold,n-1)
                    for j in range(j0+1,end+1):
                        y=sd[j]
                        if not y: continue
                        if y[0]>=pa+k: out=(y[0] if False else pa+k)-pa-ef; break
                        # hedge: buy the opposite side at its ask (= 1 - this side's bid); locks 1 - cost
                        opp=1-y[0]; lock=1-pa-opp-ef-fee(opp)
                        if lock>0: out=lock; locks+=1; break
                    if out is None:
                        y=next((sd[j] for j in range(end,j0,-1) if sd[j]),None)
                        if not y: i=end+1; continue
                        out=y[0]-pa-ef-fee(max(y[0],0.01))
                    res.append(out); wins+=out>0; i=end+1
    return res,locks,windows
print('mode maker k | trades mean_c win% locks  net_c per window (4-yield windows)')
for mode in ("dip","lead","either"):
    for maker in (False,True):
        for k in (0.03,0.05):
            r,l,w=run(mode,k,maker)
            if r: print(f"{mode:6} {'maker' if maker else 'taker'} {k:.2f} | {len(r):5d} {statistics.mean(r)*100:+6.2f} {sum(1 for x in r if x>0)/len(r)*100:4.0f}% {l:4d}  {sum(r)*100/w:+7.2f}")
print('DONE')
