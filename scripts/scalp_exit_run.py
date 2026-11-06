"""How much can the early trend scalp win? Same 241-entry replay as scalp_wave_test.py (that docstring follows), exits compared: hold, fixed takes, a trailing sale once +10c with a crossing is reached, half at +10c.

Does the wave just before the buy separate winning early-trend scalps from losing ones? Replays crypto_trend_scalp_paper on 11-03 onward: one entry per window at the first 10 s step 30-240 s after the open where
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
        # the entry's own-side bid path after the buy, with the strike crossings so far at each quote
        a0=max(0,int(o)-G0); cum=0; prev=None; kk=a0; path=[]
        for j2 in range(i+1,len(q)):
            if q[j2][0]>=c-15: break
            k2=int(q[j2][0])-G0
            while kk<=k2 and kk<len(g):
                v=g[kk]
                if v is not None:
                    sg=v>=K
                    if prev is not None and sg!=prev: cum+=1
                    prev=sg
                kk+=1
            path.append((q[j2][1] if side=='yes' else 1-q[j2][2],cum))
        # crossings between the open and the buy are counted into cum's starting value by walking the grid first
        rows.append(dict(c=c,ask=ask0,side=side,won=(w['result']==side),path=path,pre=0))
        break
print('entries',len(rows))
def pnl(x,exit_px,frac=1.0):
    """net dollars per contract: frac sold at exit_px (fee on that leg), the rest held to the result; the buy fee on the whole"""
    a=x['ask']; held=(1-a) if x['won'] else -a
    sold=(exit_px-a)-fee(max(exit_px,0.01)) if exit_px is not None else None
    if sold is None: return held-fee(a)
    return frac*sold+(1-frac)*held-fee(a)
def run(x,mode,p1=0.10,p2=0.0):
    a=x['ask']; armed=False; peak=0.0
    for b,cr in x['path']:
        if mode=='hold': return pnl(x,None)
        if mode=='fixed':
            if cr>=1 and b>=a+p1-1e-9: return pnl(x,b)
        elif mode=='half':
            if cr>=1 and b>=a+p1-1e-9: return pnl(x,b,0.5)
        elif mode=='trail':
            if not armed and cr>=1 and b>=a+p1-1e-9: armed=True; peak=b
            elif armed:
                peak=max(peak,b)
                if b<=peak-p2: return pnl(x,b)
    return pnl(x,None)
import statistics as S
def line(name,mode,p1=0.10,p2=0.0):
    h=sorted(rows,key=lambda x:x['c']); n=len(h); A,B=h[:n//2],h[n//2:]
    tk=lambda L: S.mean(run(x,mode,p1,p2)/x['ask']*10 for x in L)
    allx=S.mean(run(x,mode,p1,p2)/x['ask']*10 for x in rows)
    wins=sum(1 for x in rows if run(x,mode,p1,p2)>0)
    print(f"  {name:44} win {100*wins/n:3.0f}%  net/$10 {allx:+5.2f} | halves {tk(A):+5.2f} {tk(B):+5.2f}")
print('exits on the early trend scalp replay (crossing >= 1 needed for every sale):')
line('hold to the result','hold')
for t_ in (0.08,0.10,0.15,0.20,0.30): line(f'fixed take +{t_:.2f}','fixed',t_)
for d in (0.04,0.06,0.10): line(f'trail {d:.2f} from the peak after +0.10','trail',0.10,d)
for d in (0.05,0.08): line(f'trail {d:.2f} after +0.15','trail',0.15,d)
line('half at +0.10, half held to the result','half',0.10)
line('half at +0.15, half held to the result','half',0.15)
print('DONE')
