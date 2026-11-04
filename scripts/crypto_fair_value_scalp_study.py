import os, time, bisect, math, collections, statistics, random, calendar
os.nice(15)
from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
from kalshi_moonshot import variants
s=config.reload(); now=time.time(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
t0=calendar.timegm((2026,10,28,5,0,0))
IDX={'KXBTC15M':'BRTI','KXETH15M':'ETHUSD_RTI','KXSOL15M':'SOLUSD_RTI','KXDOGE15M':'DOGEUSD_RTI'}
Phi=lambda z:0.5*(1+math.erf(z/math.sqrt(2)))
W=st.db.execute("SELECT ticker,series,close_ts,strike FROM windows WHERE series IN ('KXBTC15M','KXETH15M','KXSOL15M','KXDOGE15M') AND result IN ('yes','no') AND strike>0 AND close_ts>? ORDER BY close_ts",(t0,)).fetchall()
random.seed(5); W=list(W); random.shuffle(W); W=W[:700]
STEP=5
series=[]   # per window: list of (t_left, mid, bid, ask, pstar)
tickcache={}
def ticks(ix):
    if ix not in tickcache:
        r=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(ix,t0-600)).fetchall()
        tickcache[ix]=([x[0] for x in r],[x[1] for x in r])
    return tickcache[ix]
for w in W:
    close=float(w['close_ts']); K=float(w['strike']); ts_,vs=ticks(IDX[w['series']])
    q=tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",(w['ticker'],close-910,close)).fetchall()
    if len(q)<60: continue
    qt=[x[0] for x in q]; rows=[]
    for left in range(840,90,-STEP):
        t=close-left
        k=bisect.bisect_right(qt,t)-1
        if k<0 or t-qt[k]>15: continue
        j=bisect.bisect_right(ts_,t)-1; j0=bisect.bisect_right(ts_,t-300)-1
        if j<0 or j0<0 or t-ts_[j]>10 or j-j0<60: continue
        seg=vs[j0:j+1]; sq=sum(math.log(b/a)**2 for a,b in zip(seg,seg[1:])); dt=ts_[j]-ts_[j0]
        if sq<=0 or dt<=0: continue
        rate=math.sqrt(sq/dt); S=vs[j]
        z=math.log(S/K)/(rate*math.sqrt(left)); ps=Phi(z)
        snap=variants.snapshot(ts_,vs,K,'yes',t,close)
        rows.append((left,(q[k][1]+q[k][2])/2,float(q[k][1]),float(q[k][2]),ps,z,(snap['variety'] if snap else '?')))
    if len(rows)>40: series.append(rows)
print('windows',len(series))
# 1. does the fair-value gap predict the next move?
X=[];Y=[]
H=6
for rows in series:
    for i in range(len(rows)-H):
        l,mid,b,a,ps,z,_v=rows[i]; l2,mid2,*_=rows[i+H]
        if l-l2!=H*STEP: continue
        if 0.1<mid<0.9: X.append(ps-mid); Y.append(mid2-mid)
print('samples',len(X),' corr(next 30s move, fair-value gap) =',round(statistics.correlation(X,Y),3))
cut=lambda lo,hi:[y for x,y in zip(X,Y) if lo<=x<hi]
for lo,hi in ((-9,-0.15),(-0.15,-0.08),(-0.08,-0.04),(-0.04,0.04),(0.04,0.08),(0.08,0.15),(0.15,9)):
    v=cut(lo,hi)
    if v: print(f"  gap {lo:+.2f}..{hi:+.2f}  n{len(v):5d}  mean next-30s move {statistics.mean(v)*100:+5.2f}c   (cost to trade about 5c)")
def dipsim(variety_set,k,dip=0.06,hold=12,stop=0.08):
    res=[]
    for rows in series:
        i=6
        while i<len(rows)-hold-1:
            l,mid,b,a,ps,z,v=rows[i]
            if v not in variety_set or not (0.15<mid<0.85) or l>780 or l<150: i+=1; continue
            prevmax=max(r[3] for r in rows[i-6:i])           # yes ask over the last 30 s
            prevmin=min(r[2] for r in rows[i-6:i])           # yes bid over the last 30 s
            for yes in (True,False):
                pa=a if yes else 1-b
                prev_ask=max((r[3] if yes else 1-r[2]) for r in rows[i-6:i])
                if pa>prev_ask-dip: continue                 # the contract we would buy has fallen by `dip`
                ef=fee(pa); out=None; end=i+hold
                for j in range(i+1,end+1):
                    _,_,b2,a2,*_=rows[j]; bn=b2 if yes else 1-a2
                    if bn>=pa+k: out=k-ef; break
                    if bn<=pa-stop: out=bn-pa-ef-fee(max(bn,.01)); break
                if out is None:
                    _,_,b2,a2,*_=rows[end]; bn=b2 if yes else 1-a2; out=bn-pa-ef-fee(max(bn,.01))
                res.append(out); break
            i+=hold if res and False else 1
    return res
print('dip scalps by variety (buy the side that fell 6c in 30 s; taker in; resting sell +k; stop 8c; 60 s)')
for name,vs_ in (('A',{'A'}),('B',{'B'}),('C',{'C'}),('D',{'D'}),('E',{'E'}),('F',{'F'}),('G oscillating',{'G'}),('all',set('ABCDEFG?'))):
    for k in (0.04,0.08):
        r=dipsim(vs_,k)
        if len(r)>=40: print(f"  {name:14} k{k:.2f} | {len(r):5d} trades  mean {statistics.mean(r)*100:+6.2f}c  win {sum(1 for x in r if x>0)/len(r)*100:3.0f}%")
print('DONE')
