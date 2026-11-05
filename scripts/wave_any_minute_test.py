"""Does the wave rule need the 7-13 minute entry window? Every crypto window since 10/26, every 20 s step from minute 1 to 14.5: the cheap side (8-20c ask, spread <= 0.03) in the wild regime (6 h vol >= 1.3x the coin's median),
held to settlement, $10 ticket, net of the taker fee. One entry per window per minute bucket (the first qualifying step), so a bucket reads like the lane's first-moment buy. Wave: amp and pos as variants.wave_features
(bet side's price over the last 300 s in typical minutes; amp = range of the last 180 s; pos = where the buy sits in it). Reported by entry minute, with and without the lane's gate (amp >= 2, pos <= 0.5)."""
import os, time, bisect, math, calendar, collections, statistics as S
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True); now=time.time()
t0=calendar.timegm((2026,10,26,5,0,0)); G0=int(t0-8*3600); G1=int(now)
COIN={'KXBTC15M':'BTC','KXETH15M':'ETH','KXSOL15M':'SOL','KXXRP15M':'XRP','KXDOGE15M':'DOGE'}
LG={}; PS={}; MIN={}
for ser,coin in COIN.items():
    r=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(variants.crypto_index(ser),G0-60)).fetchall()
    ts=[float(x[0]) for x in r]; vs=[float(x[1]) for x in r]; g=[]; j=0; last=None
    for T in range(G0,G1):
        while j<len(ts) and ts[j]<=T: last=vs[j]; j+=1
        g.append(last)
    first=next((i for i,x in enumerate(g) if x),None)
    lg=[math.log(x) if x else (math.log(g[first]) if first is not None else 0.0) for x in g]
    P=[0.0]
    for a,b in zip(lg,lg[1:]): P.append(P[-1]+(b-a)**2)
    LG[coin]=lg; PS[coin]=P; MIN[coin]=[lg[i] for i in range(0,len(lg),60)]
def vol6h(coin,k):
    m=MIN[coin]; e=k//60
    if e<361: return None
    rets=[m[i+1]-m[i] for i in range(e-360,e)]
    return S.pstdev(rets)*math.sqrt(60)*1e4
# per-coin median of vol6h, sampled hourly
med={}
for coin in COIN.values():
    v=[vol6h(coin,k) for k in range(30000,len(LG[coin]),3600)]; v=[x for x in v if x]; med[coin]=S.median(v)
W=st.db.execute("SELECT ticker,series,close_ts,result FROM windows WHERE close_ts>? AND result IN ('yes','no') ORDER BY close_ts",(t0+1800,)).fetchall()
BUCKETS=((1,3),(3,5),(5,7),(7,10),(10,13),(13,14.5))
rows=[]
for w in W:
    coin=COIN.get(w['series'])
    if not coin: continue
    c=float(w['close_ts']); o=c-900; lg=LG[coin]; P=PS[coin]
    kopen=int(o)-G0
    if kopen<22000 or kopen>=len(lg)-900: continue
    v=vol6h(coin,kopen)
    if v is None or v/med[coin]<1.3: continue
    q=[(float(a),float(b),float(cc)) for a,b,cc in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND bid>0 AND ask>0 ORDER BY ts",(w['ticker'],)).fetchall()]
    if len(q)<40: continue
    qt=[x[0] for x in q]; done=set()
    for el in range(60,871,20):
        mnt=el/60; b=next((i for i,(lo,hi) in enumerate(BUCKETS) if lo<=mnt<hi),None)
        if b is None or b in done: continue
        t=o+el; i=bisect.bisect_right(qt,t)-1
        if i<0 or t-qt[i]>5: continue
        yb,ya=q[i][1],q[i][2]
        for side,bid0,ask0 in (('yes',yb,ya),('no',1-ya,1-yb)):
            if not (0.08<=ask0<=0.20) or ask0-bid0>0.03: continue
            k=int(t)-G0; u=1.0 if side=='yes' else -1.0
            sq=P[k]-P[k-300]
            if sq<=0: continue
            unit=math.sqrt(sq/300)*math.sqrt(60)
            seg=[u*(lg[j]-lg[k-300])/unit for j in range(k-179,k+1)]
            hi_,lo_=max(seg),min(seg); amp=hi_-lo_; pos=(seg[-1]-lo_)/amp if amp>0 else 0.5
            won=w['result']==side; cn=10/ask0
            pnl=((1-ask0)*cn if won else -10.0)-fee(ask0)*cn
            rows.append(dict(b=b,amp=amp,pos=pos,pnl=pnl,won=won,close=c,coin=coin,gate=(amp>=2.0 and pos<=0.5)))
            done.add(b); break
print('wild-regime windows scanned; entries',len(rows))
def line(name,sub):
    n=len(sub)
    if n<15: print(f'  {name:34} n{n}'); return
    h=sorted(sub,key=lambda x:x['close']); a,b=h[:n//2],h[n//2:]
    f=lambda z: f"{100*sum(x['won'] for x in z)/len(z):4.1f}%/{sum(x['pnl'] for x in z)/len(z):+5.2f}"
    print(f"  {name:34} n{n:4d} won {100*sum(x['won'] for x in sub)/n:4.1f}%  net/$10 {sum(x['pnl'] for x in sub)/n:+6.2f} | halves {f(a)}  {f(b)}")
for b,(lo,hi) in enumerate(BUCKETS):
    L=[x for x in rows if x['b']==b]
    print(f'minute {lo}-{hi}')
    line('every cheap-side entry',L); line('wave gate passes (amp>=2, pos<=.5)',[x for x in L if x['gate']]); line('wave gate fails',[x for x in L if not x['gate']])
print('all minutes pooled')
line('every entry',rows); line('gate passes',[x for x in rows if x['gate']]); line('gate fails',[x for x in rows if not x['gate']])
print('DONE')
