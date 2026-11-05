import os, time, calendar, bisect, json, math, itertools
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
t0=calendar.timegm((2026,11,2,6,0,0)); now=time.time()
W=st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY close_ts",(t0,)).fetchall()
G0=int(t0-4300); G1=int(now)+2
LOOKS=(60,120,300,900,3600)
grids={}
def grid(ix):
    if ix in grids: return grids[ix]
    r=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(ix,G0-60)).fetchall()
    ts=[float(x[0]) for x in r]; vs=[float(x[1]) for x in r]
    g=[]; j=0; last=None
    for T in range(G0,G1):
        while j<len(ts) and ts[j]<=T: last=vs[j]; j+=1
        g.append(last)
    P=[0.0]
    for a,b in itertools.pairwise(g): P.append(P[-1]+((math.log(b/a))**2 if a and b else 0.0))
    grids[ix]=(ts,vs,g,P); return grids[ix]
def letter(z,slope,cross,thr):
    if cross>=thr: return 'G'
    if z>=1.0: return 'A' if slope>=0.5 else 'C' if slope<=-0.5 else 'B'
    return 'D' if slope<=-0.5 else 'F' if slope>=0.5 else 'E'
THR={L:max(2,round(0.8*L/60)) for L in LOOKS}
rows=[]; wid=0
for w in W:
    ser=w['series']
    if ser in variants.RECORD_ONLY: continue
    cx=variants.crypto_index(ser); ix=cx or variants.biquote_index(ser)
    if not ix: continue
    close=float(w['close_ts']); opened=close-900; ts,vs,g,P=grid(ix); K=float(w['strike'])
    if not cx:
        i0=bisect.bisect_left(ts,opened)
        if i0>=len(ts) or ts[i0]-opened>20: continue
        K=vs[i0]
    q=[(float(a),float(b),float(c)) for a,b,c in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",(w['ticker'],opened-20,close)).fetchall()]
    if len(q)<20: continue
    qt=[x[0] for x in q]; wid+=1; coin=variants.coin_of(ser)
    ko=int(opened)-G0; kstart=ko-3700
    if kstart<0 or int(close)-G0>=len(g): continue
    X=[0]*(int(close)-G0-kstart+2); prev=None
    for n,k in enumerate(range(kstart,int(close)-G0+1)):
        v=g[k]; sgn=1 if (v is not None and v>K) else 0
        X[n+1]=X[n]+(1 if (prev is not None and sgn!=prev) else 0); prev=sgn
    def cross(k,L): return X[k-kstart+1]-X[k-L-kstart+1]
    for e in range(60,841,30):
        at=opened+e; i=bisect.bisect_right(qt,at)-1
        if i<0 or at-qt[i]>20: continue
        _,yb,ya=q[i]; k=int(at)-G0
        if g[k] is None: continue
        left=close-at
        for side,bid,ask in (('yes',yb,ya),('no',1-ya,1-yb)):
            if not (0.1<=ask<=0.95): continue
            sg=1 if side=='yes' else -1; c=math.log(g[k]/K)*sg
            lets=''; zs=[]; ok=True
            for L in LOOKS:
                rate=math.sqrt((P[k]-P[k-L])/L)
                if rate<=0 or g[k-L] is None: ok=False; break
                z=c/(rate*math.sqrt(left)); slope=(c-math.log(g[k-L]/K)*sg)/(rate*math.sqrt(L))
                lets+=letter(z,slope,cross(k,L),THR[L]); zs.append(round(z,2))
            if not ok: continue
            won=w['result']==side; cc=10/ask; pnl=((1-ask)*cc if won else -10)-fee(ask)*cc
            rows.append((wid,'c' if cx else 'm',coin,e,side[0],round(ask,3),round(ask-bid,3),int(won),round(pnl,3),close,lets,zs))
json.dump(rows,open('/tmp/ms.json','w'))
print(len(rows),'rows; windows',wid,'DONE')
