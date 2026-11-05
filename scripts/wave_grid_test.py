"""The swing shape over the last 3-5 minutes (the oscillation the user reads on the chart) against the wild-regime underdog and the early trend scalp. For each entry the bet side's price path over the last 240 s
(second-by-second index, in typical-minute units) is cut into zigzag legs of 0.5 typical minutes or more: legs (count), amp (range of the last 180 s), pos (where the buy sits in that range, 0 = at the bet side's
low), last_dir (+1 = the running leg is toward the bet), last_size. Buckets show win rate / net with both halves."""
import math
def zig(c,thr):
    if not c: return []
    legs=[]; d=0; ext=c[0]; start=c[0]
    for x in c[1:]:
        if d>=0 and x>ext: ext=x; d=1 if x-start>=thr else d
        if d>0 and x>ext: ext=x
        if d>0 and ext-x>=thr: legs.append((1,ext-start)); start=ext; ext=x; d=-1; continue
        if d<=0 and x<ext: ext=x; d=-1 if start-x>=thr else d
        if d<0 and x<ext: ext=x
        if d<0 and x-ext>=thr: legs.append((-1,start-ext)); start=ext; ext=x; d=1; continue
    legs.append((d or 1,abs(ext-start)))
    return legs
def wave(path,u,unit):
    """path: log prices oldest->newest at 1 s over >=240 s; returns the swing features toward the bet side"""
    c=[u*(x-path[0])/unit for x in path[-240:]]
    last=c[-180:]; hi,lo=max(last),min(last)
    lg=zig(c[::3],0.5)
    return dict(legs=len(lg),amp=hi-lo,pos=(c[-1]-lo)/(hi-lo) if hi>lo else 0.5,ldir=lg[-1][0] if lg else 0,lsize=lg[-1][1] if lg else 0.0)
def buckets(rows,show):
    show('all',rows)
    for lo,hi in ((0,2),(2,3),(3,4),(4,99)): show(f'legs in [{lo},{hi})',[x for x in rows if lo<=x['legs']<hi])
    for lo,hi in ((0,1),(1,2),(2,99)): show(f'amp in [{lo},{hi}) typical min',[x for x in rows if lo<=x['amp']<hi])
    for lo,hi in ((0,0.2),(0.2,0.5),(0.5,0.8),(0.8,1.01)): show(f'pos in range [{lo},{hi})',[x for x in rows if lo<=x['pos']<hi])
    show('swinging (legs>=3, amp>=1) and at the low (pos<0.3)',[x for x in rows if x['legs']>=3 and x['amp']>=1 and x['pos']<0.3])
    show('swinging (legs>=3, amp>=1) and at the high (pos>0.7)',[x for x in rows if x['legs']>=3 and x['amp']>=1 and x['pos']>0.7])
    show('running leg toward the bet and big (>=1)',[x for x in rows if x['ldir']>0 and x['lsize']>=1])
    show('running leg against the bet and big (>=1)',[x for x in rows if x['ldir']<0 and x['lsize']>=1])
import os, time, bisect, calendar, statistics as S
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True); now=time.time()

# ---------- A. wild-regime underdog (same setups as crypto_wild_fade_study) ----------
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
    return S.pstdev(rets)*math.sqrt(60)*1e4 if len(rets)>200 else None
for r in recs: r['v']=vol6h(r['coin'],r['ts'])
recs=[r for r in recs if r['v']]
med={c:S.median(r['v'] for r in recs if r['coin']==c) for c in IDX if any(r['coin']==c for r in recs)}
sel=[r for r in recs if r['v']/med[r['coin']]>=1.3 and r['ask']>=0.82]
def at(ts,vs,t):
    i=bisect.bisect_right(ts,t)-1
    return vs[i] if i>=0 and t-ts[i]<20 else None
A=[]
for r in sel:
    ts,vs=ser[r['coin']]; t=r['ts']; u=-1.0 if r['side']=='yes' else 1.0   # underdog direction
    px=[at(ts,vs,t-k) for k in range(300,-1,-1)]
    if any(p is None for p in px): continue
    lg=[math.log(p) for p in px]
    unit=math.sqrt(sum((lg[i+1]-lg[i])**2 for i in range(300))/300)*math.sqrt(60)
    if unit<=0: continue
    f=wave(lg,u,unit); f.update(uw=1-r['won'],pnl=r['flip_pnl'],close=r['close_ts']); A.append(f)
print('A. WILD UNDERDOG: setups',len(sel),'with a path',len(A))
def showA(name,sub):
    n=len(sub)
    if n<10: print(f'  {name:56} n{n}'); return
    h=sorted(sub,key=lambda x:x['close']); a,b=h[:n//2],h[n//2:]
    f=lambda z: f"{sum(x['uw'] for x in z)/len(z)*100:4.1f}%/{sum(x['pnl'] for x in z)/len(z):+.2f}"
    print(f"  {name:56} n{n:4d} won {sum(x['uw'] for x in sub)/n*100:4.1f}%  flip {sum(x['pnl'] for x in sub)/n:+6.2f}/bet | halves {f(a)}  {f(b)}")
buckets(A,showA)


print('\nGRID: the lane rule (amp >= A and pos <= P) on the wild underdog, so the chosen 2.0 / 0.5 can be compared with its neighbours')
print(f"{'amp>=':>6} {'pos<=':>6} {'n':>5} {'won%':>6} {'flip/bet':>9}  halves (won%/flip)")
for amin in (0,1,1.5,2,2.5,3):
    for pmax in (0.3,0.4,0.5,0.6,0.7,1.01):
        sub=[x for x in A if x['amp']>=amin and x['pos']<=pmax]; n=len(sub)
        if n<30: continue
        h=sorted(sub,key=lambda x:x['close']); a,b=h[:n//2],h[n//2:]
        f=lambda z: f"{sum(x['uw'] for x in z)/len(z)*100:4.1f}/{sum(x['pnl'] for x in z)/len(z):+5.2f}"
        mark='  <- chosen' if (amin==2 and pmax==0.5) else '  <- no gate' if (amin==0 and pmax>1) else ''
        print(f"{amin:6g} {pmax:6.2f} {n:5d} {sum(x['uw'] for x in sub)/n*100:6.1f} {sum(x['pnl'] for x in sub)/n:+9.2f}  {f(a)}  {f(b)}{mark}")
print('DONE')
