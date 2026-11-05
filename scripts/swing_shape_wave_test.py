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

# ---------- B. early trend scalp replay (as scalp_wave_test.py) ----------
G0=int(calendar.timegm((2026,11,3,0,0,0))-4000); G1=int(now); T0=G0+4000
def grid(ix):
    r=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(ix,G0-60)).fetchall()
    ts=[float(x[0]) for x in r]; vs=[float(x[1]) for x in r]; g=[]; j=0; last=None
    for T in range(G0,G1):
        while j<len(ts) and ts[j]<=T: last=vs[j]; j+=1
        g.append(last)
    return g
G={sr:grid(variants.crypto_index(sr)) for sr in variants.BROAD_SERIES}
def broad(k):
    mv=[]
    for sr in variants.BROAD_SERIES:
        a,b=G[sr][k],G[sr][k-3600]
        if not a or not b: return None
        mv.append(100*(a/b-1))
    return sum(mv)/len(mv)
W=st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY close_ts",(T0+1800-1800,)).fetchall()
B=[]
for w in W:
    sr=w['series']
    if sr not in variants.BROAD_SERIES: continue
    g=G[sr]; c=float(w['close_ts']); o=c-900; K=float(w['strike'])
    q=[(float(a),float(b),float(cc)) for a,b,cc in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND bid>0 AND ask>0 ORDER BY ts",(w['ticker'],)).fetchall()]
    if len(q)<40: continue
    qt=[x[0] for x in q]
    for el in range(30,241,10):
        t=o+el; k=int(t)-G0
        if k<3700 or k>=len(g) or g[k] is None: continue
        i=bisect.bisect_right(qt,t)-1
        if i<0 or t-qt[i]>5: continue
        tr=broad(k)
        if tr is None: continue
        side='no' if tr<=-0.5 else 'yes' if tr>=0.5 else None
        if side is None: continue
        bid0,ask0=(q[i][1],q[i][2]) if side=='yes' else (1-q[i][2],1-q[i][1])
        if not (0.50<=ask0<=0.70) or ask0-bid0>0.04: continue
        seg=[g[k-j] for j in range(300,-1,-1)]
        if any(x is None for x in seg): continue
        lg=[math.log(x) for x in seg]
        unit=math.sqrt(sum((lg[j+1]-lg[j])**2 for j in range(300))/300)*math.sqrt(60)
        if unit<=0: continue
        f=wave(lg,1.0 if side=='yes' else -1.0,unit)
        cross=0; prev=None; kk=max(0,int(o)-G0)
        while kk<=k:
            v=g[kk]
            if v is not None:
                sg=v>=K
                if prev is not None and sg!=prev: cross+=1
                prev=sg
            kk+=1
        sold=None; j2=i+1
        while j2<len(q) and q[j2][0]<c-15:
            k2=int(q[j2][0])-G0
            while kk<=k2 and kk<len(g):
                v=g[kk]
                if v is not None:
                    sg=v>=K
                    if prev is not None and sg!=prev: cross+=1
                    prev=sg
                kk+=1
            b=q[j2][1] if side=='yes' else 1-q[j2][2]
            if cross>=1 and b>=ask0+0.10: sold=b; break
            j2+=1
        won=(w['result']==side)
        cents=100*(sold-ask0-fee(ask0)-fee(max(sold,0.01))) if sold is not None else 100*(((1-ask0) if won else -ask0)-fee(ask0))
        f.update(c=c,ask=ask0,cents=cents,sold=sold is not None,win=(sold is not None) or won); B.append(f)
        break
print('\nB. EARLY TREND SCALP replay: entries',len(B))
def showB(name,sub):
    n=len(sub)
    if n<10: print(f'  {name:56} n{n}'); return
    h=sorted(sub,key=lambda x:x['c']); a,b=h[:n//2],h[n//2:]
    tk=lambda z: S.mean(x['cents']/x['ask']/100*10 for x in z)
    print(f"  {name:56} n{n:4d} good {100*sum(x['win'] for x in sub)/n:3.0f}%  net/$10 {tk(sub):+5.2f} | halves {tk(a):+5.2f} {tk(b):+5.2f}")
buckets(B,showB)
print('DONE')
