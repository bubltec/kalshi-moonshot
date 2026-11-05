import os, time, calendar, bisect, json, gzip, base64, math, itertools
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
variants.load_custom(s)
t0=calendar.timegm((2026,11,2,6,0,0)); now=time.time()
W=st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY close_ts",(t0,)).fetchall()
G0=int(t0-4200); G1=int(now)+2
grids={}
def grid(ix):
    if ix in grids: return grids[ix]
    r=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(ix,G0-60)).fetchall()
    ts=[float(x[0]) for x in r]; vs=[float(x[1]) for x in r]
    g=[]; j=0; last=None
    for T in range(G0,G1):
        while j<len(ts) and ts[j]<=T: last=vs[j]; j+=1
        g.append(last)
    P=[0.0]; 
    for a,b in itertools.pairwise(g):
        P.append(P[-1]+((math.log(b/a))**2 if a and b else 0.0))
    grids[ix]=(ts,vs,g,P); return grids[ix]
def rate(P,k,N):
    if k-N<0: return None
    return math.sqrt((P[k]-P[k-N])/N)
rows=[]; wid=0
for w in W:
    ser=w['series']
    if ser in variants.RECORD_ONLY: continue
    cx=variants.crypto_index(ser); ix=cx or variants.biquote_index(ser)
    if not ix: continue
    close=float(w['close_ts']); ts,vs,g,P=grid(ix); K=float(w['strike'])
    if not cx:
        i0=bisect.bisect_left(ts,close-900)
        if i0>=len(ts) or ts[i0]-(close-900)>20: continue
        K=vs[i0]
    q=[(float(a),float(b),float(c)) for a,b,c in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",(w['ticker'],close-500,close-90)).fetchall()]
    if not q: continue
    qt=[x[0] for x in q]; wid+=1
    for L in range(480,119,-10):
        at=close-L; i=bisect.bisect_right(qt,at)-1
        if i<0 or at-qt[i]>15: continue
        _,yb,ya=q[i]
        for side,bid,ask in (('yes',yb,ya),('no',1-ya,1-yb)):
            if 0.76<=ask<=0.92 and ask-bid<=0.03+1e-9:
                k=int(at)-G0
                if k<3600 or k>=len(g) or g[k] is None: break
                opened=int(close-900)-G0; N15=min(900,max(60,k-opened)) if k>opened else 900
                snap=variants.snapshot(ts,vs,K,side,at,close)
                if not snap: break
                sign=1.0 if side=='yes' else -1.0
                c=math.log(g[k]/K)*sign
                rr=[rate(P,k,N) for N in (120,300,900,3600)]
                if any(x is None or x<=0 for x in rr): break
                path=[float('%.4g'%(math.log(g[k-o]/K)*sign/(rr[2]*math.sqrt(60)))) for o in range(0,421,30)]
                if any(x is None or x<=0 for x in rr): break
                won=w['result']==side; cc=10/ask; pnl=((1-ask)*cc if won else -10)-fee(ask)*cc
                rows.append(dict(w=wid,cls='crypto' if cx else 'commodity',coin=variants.coin_of(ser),left=L,side=side,ask=round(ask,3),won=int(won),pnl=round(pnl,3),close=close,c=c,r=[float(f'{x:.6g}') for x in rr],path=path,crosses=snap['crosses'],var=snap['variety'],z=round(snap['z'],3)))
                break
json.dump(rows,open('/tmp/paths.json','w'))
b=base64.b64encode(gzip.compress(open('/tmp/paths.json','rb').read())).decode()
open('/tmp/paths.b64','w').write('\n'.join(b[i:i+1000] for i in range(0,len(b),1000))+'\n')
print(len(rows),'obs; windows',wid,'; lines',len(b)//1000+1,'DONE')
