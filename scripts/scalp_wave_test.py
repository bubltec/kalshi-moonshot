"""Does the wave just before the buy separate winning early-trend scalps from losing ones? Replays crypto_trend_scalp_paper on 11-03 onward: one entry per window at the first 10 s step 30-240 s after the open where
the market-wide 60-minute move (five coins) is -0.5% or lower and the NO side is 0.50-0.70 with a spread of 0.04 or less, or +0.5% or higher and YES likewise; sell when the bid is 0.10 over the paid price AND
the index has crossed the strike at least once since the open, otherwise hold to the result. Wave features (second-by-second index, toward the bet side, in typical-minute units): d30 = last 30 s, prior = the 90 s
before that, rise = how far the bet side has come up from its 4-minute low. Net per $10 ticket after both fees; halves by close time."""
import os, calendar, bisect, collections, math, itertools, time, statistics as S
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True); now=time.time()
t0=calendar.timegm((2026,11,3,0,0,0)); G0=int(t0-4000); G1=int(now)
def grid(ix):
    r=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(ix,G0-60)).fetchall()
    ts=[float(x[0]) for x in r]; vs=[float(x[1]) for x in r]; g=[]; j=0; last=None
    for T in range(G0,G1):
        while j<len(ts) and ts[j]<=T: last=vs[j]; j+=1
        g.append(last)
    return g
G={}
for ser in variants.BROAD_SERIES: G[ser]=grid(variants.crypto_index(ser))
def broad(k):
    mv=[]
    for ser in variants.BROAD_SERIES:
        a,b=G[ser][k],G[ser][k-3600]
        if not a or not b: return None
        mv.append(100*(a/b-1))
    return sum(mv)/len(mv)
W=st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY close_ts",(t0+1800,)).fetchall()
rows=[]
for w in W:
    ser=w['series']
    if ser not in variants.BROAD_SERIES: continue
    g=G[ser]; c=float(w['close_ts']); o=c-900; K=float(w['strike'])
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
        seg=[g[k-j] for j in range(0,301)]
        if any(x is None for x in seg): continue
        lg=[math.log(x) for x in seg]   # newest first
        sq=sum((lg[j]-lg[j+1])**2 for j in range(300)); unit=math.sqrt(sq/300)*math.sqrt(60)
        if unit<=0: continue
        u=1.0 if side=='yes' else -1.0
        d30=u*(lg[0]-lg[30])/unit; prior=u*(lg[30]-lg[120])/unit
        cu=[u*(lg[j]-lg[300]) for j in range(0,241,5)]; rise=(cu[0]-min(cu))/unit
        # exit: crossings of the strike since the open (index vs strike), bid 0.10 over price
        a0=max(0,int(o)-G0); cross=0; prev=None
        for kk in range(a0,k+1):
            v=g[kk]
            if v is None: continue
            sg=v>=K
            if prev is not None and sg!=prev: cross+=1
            prev=sg
        sold=None; j2=i+1; kk=k
        while j2<len(q) and q[j2][0]<c-15:
            tq=q[j2][0]; k2=int(tq)-G0
            while kk<k2 and kk<len(g)-1:
                kk+=1; v=g[kk]
                if v is None: continue
                sg=v>=K
                if prev is not None and sg!=prev: cross+=1
                prev=sg
            b=q[j2][1] if side=='yes' else 1-q[j2][2]
            if cross>=1 and b>=ask0+0.10: sold=b; break
            j2+=1
        won=(w['result']==side)
        if sold is not None: cents=100*(sold-ask0-fee(ask0)-fee(max(sold,0.01)))
        else: cents=100*(((1-ask0) if won else -ask0)-fee(ask0))
        hold=100*(((1-ask0) if won else -ask0)-fee(ask0))
        rows.append(dict(c=c,d30=d30,prior=prior,rise=rise,ask=ask0,cents=cents,sold=sold is not None,win=(sold is not None) or won,hold=hold))
        break
print('entries',len(rows))
def show(name,sub):
    n=len(sub)
    if n<10: print(f'  {name:46} n{n}'); return
    h=sorted(sub,key=lambda x:x['c']); a,b=h[:n//2],h[n//2:]
    m=lambda z,f: S.mean(x[f] for x in z)
    ticket=lambda z: S.mean(x['cents']/x['ask']/100*10 for x in z)   # net per $10 ticket
    print(f"  {name:46} n{n:4d} good {100*sum(x['win'] for x in sub)/n:3.0f}% sold-early {100*sum(x['sold'] for x in sub)/n:3.0f}%  net/$10 {ticket(sub):+5.2f} (hold-only {S.mean(x['hold']/x['ask']/100*10 for x in sub):+5.2f}) | halves {ticket(a):+5.2f} {ticket(b):+5.2f}")
show('all entries',rows)
for lo,hi in ((-9,-0.5),(-0.5,0),(0,0.5),(0.5,1),(1,9)): show(f'd30 toward the bet in [{lo},{hi})',[x for x in rows if lo<=x['d30']<hi])
for lo,hi in ((-9,-0.5),(-0.5,0.5),(0.5,9)): show(f'prior in [{lo},{hi})',[x for x in rows if lo<=x['prior']<hi])
show('dip then turn: prior<-0.3 and d30>0.3',[x for x in rows if x['prior']<-0.3 and x['d30']>0.3])
show('moving with the bet: prior>0.3 and d30>0.3',[x for x in rows if x['prior']>0.3 and x['d30']>0.3])
show('moving against: prior<-0.3 and d30<-0.3',[x for x in rows if x['prior']<-0.3 and x['d30']<-0.3])
for lo,hi in ((0,0.5),(0.5,1),(1,2),(2,99)): show(f'rise from 4-min low in [{lo},{hi})',[x for x in rows if lo<=x['rise']<hi])
print('DONE')
