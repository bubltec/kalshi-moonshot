import os, calendar, bisect, collections, math, statistics as S, itertools, time
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True); now=time.time()
t0=calendar.timegm((2026,11,3,0,0,0)); G0=int(t0-1800); G1=int(now)
W=st.db.execute("SELECT ticker,series,close_ts FROM windows WHERE close_ts>? AND result IN ('yes','no') ORDER BY close_ts",(t0+1800,)).fetchall()
bys=collections.defaultdict(list)
for w in W:
    if w['series'] in variants.RECORD_ONLY: continue
    bys[w['series']].append((float(w['close_ts'])-900,float(w['close_ts']),w['ticker']))
def sidepx(row,side): return (row[1],row[2]) if side=='yes' else (1-row[2],1-row[1])
rows_out=[]
for ser,wl in bys.items():
    cx=variants.crypto_index(ser); ix=cx or variants.biquote_index(ser)
    if not ix: continue
    cls='crypto' if cx else 'commodity'
    r=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(ix,G0-60)).fetchall()
    ts=[float(x[0]) for x in r]; vs=[float(x[1]) for x in r]
    g=[]; j=0; last=None
    for T in range(G0,G1):
        while j<len(ts) and ts[j]<=T: last=vs[j]; j+=1
        g.append(last)
    lg=[math.log(x) if x else None for x in g]
    P=[0.0]
    for a,b in itertools.pairwise(lg): P.append(P[-1]+((b-a)**2 if a is not None and b is not None else 0.0))
    for (o,c,tk) in wl:
        q=[(float(a),float(b),float(cc)) for a,b,cc in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND bid>0 AND ask>0 ORDER BY ts",(tk,)).fetchall()]
        if len(q)<40: continue
        qt=[x[0] for x in q]
        for el in range(120,780,10):
            t=o+el; k=int(t)-G0
            if k<400 or k>=len(lg): continue
            x=lg[k]
            if x is None or lg[k-10] is None or lg[k-20] is None or lg[k-30] is None or lg[k-60] is None: continue
            rate=math.sqrt((P[k]-P[k-300])/300)
            if rate<=0: continue
            v10=(x-lg[k-10])/(rate*math.sqrt(10)); v20=(x-lg[k-20])/(rate*math.sqrt(20)); v30=(x-lg[k-30])/(rate*math.sqrt(30)); v60=(x-lg[k-60])/(rate*math.sqrt(60))
            vp=(lg[k-10]-lg[k-20])/(rate*math.sqrt(10))
            acc=v10-vp
            seg=[y for y in lg[k-60:k+1] if y is not None]; lo,hi=min(seg),max(seg)
            pos=(x-lo)/(hi-lo) if hi>lo else 0.5
            i=bisect.bisect_right(qt,t)-1
            if i<0 or t-qt[i]>5: continue
            if abs(v30)<0.5: continue
            side='no' if v30>0 else 'yes'      # fade the recent move
            bid0,ask0=sidepx(q[i],side)
            if not (0.25<=ask0<=0.75) or ask0-bid0>0.03: continue
            res={}
            for dl,H in ((0.08,60),(0.12,90)):
                lim=min(t+H,c-15)
                if lim<=t+5: res=None; break
                exit_b=None; hit=0
                j2=i+1
                while j2<len(q) and q[j2][0]<=lim:
                    b,_=sidepx(q[j2],side)
                    if b>=ask0+dl: exit_b=b; hit=1; break
                    j2+=1
                if exit_b is None:
                    k2=bisect.bisect_right(qt,lim)-1
                    if k2<=i: res=None; break
                    exit_b=sidepx(q[k2],side)[0]
                res[(dl,H)]=(100*(exit_b-ask0-fee(ask0)-fee(max(exit_b,0.01))),hit)
            if not res: continue
            rows_out.append((cls,c,v30,v10,vp,acc,v60,pos,ask0,res))
print('samples',len(rows_out))
def vb(v): a=abs(v); return 'strong (|v30|>=2)' if a>=2 else 'medium (1-2)' if a>=1 else 'mild (0.5-1)'
def db_(r):
    v30,acc=r[2],r[5]; d=acc*(1 if v30>0 else -1)   # negative = decelerating against the direction of the move
    return 'decelerating hard (<=-1)' if d<=-1 else 'decelerating (-1..-0.3)' if d<-0.3 else 'not decelerating'
print('\nFADE the recent move (buy NO after a rise, YES after a fall), entered BEFORE any confirmation; cents per contract after fees both ways')
for cls in ('crypto','commodity'):
    L=[r for r in rows_out if r[0]==cls]
    mid=sorted(r[1] for r in L)[len(L)//2]
    print(f"\n=== {cls}: {len(L)} samples. all fades at +0.08/60 s: {S.mean(r[9][(0.08,60)][0] for r in L):+.2f}c hit {100*sum(r[9][(0.08,60)][1] for r in L)/len(L):.0f}%")
    for dl,H in ((0.08,60),(0.12,90)):
        print(f"  target +{dl:.2f} within {H}s:")
        for sp in ('mild (0.5-1)','medium (1-2)','strong (|v30|>=2)'):
            for dc in ('not decelerating','decelerating (-1..-0.3)','decelerating hard (<=-1)'):
                X=[r for r in L if vb(r[2])==sp and db_(r)==dc]
                if len(X)<150: continue
                a=[r[9][(dl,H)][0] for r in X if r[1]<mid]; b=[r[9][(dl,H)][0] for r in X if r[1]>=mid]
                m=S.mean(r[9][(dl,H)][0] for r in X)
                print(f"    {sp:18} {dc:25} n={len(X):6d} {m:+.2f}c hit {100*sum(r[9][(dl,H)][1] for r in X)/len(X):3.0f}%  halves {S.mean(a):+.2f}/{S.mean(b):+.2f}")
    print('  and when the spot is at the very top/bottom of its last-60-s range (position 0.9+ for a rise / 0.1- for a fall), decelerating, target +0.08/60 s:')
    X=[r for r in L if ((r[2]>0 and r[7]>=0.9) or (r[2]<0 and r[7]<=0.1)) and db_(r)!='not decelerating']
    if len(X)>=100:
        a=[r[9][(0.08,60)][0] for r in X if r[1]<mid]; b=[r[9][(0.08,60)][0] for r in X if r[1]>=mid]
        print(f"    n={len(X)} {S.mean(r[9][(0.08,60)][0] for r in X):+.2f}c hit {100*sum(r[9][(0.08,60)][1] for r in X)/len(X):.0f}% halves {S.mean(a):+.2f}/{S.mean(b):+.2f}")
print('DONE')
