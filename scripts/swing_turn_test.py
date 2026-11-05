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
qcache={}
def quotes(tk):
    q=qcache.get(tk)
    if q is None:
        q=[(float(a),float(b),float(c)) for a,b,c in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND bid>0 AND ask>0 ORDER BY ts",(tk,)).fetchall()]
        qcache[tk]=(q,[x[0] for x in q]); return qcache[tk]
    return q
def sidepx(row,side): return (row[1],row[2]) if side=='yes' else (1-row[2],1-row[1])
TRADES=[]   # dicts
ORACLE=[]
FUT=[]      # (cls, npiv15 bucket, future pivots in next 300 s)
for ser,wl in bys.items():
    cx=variants.crypto_index(ser); ix=cx or variants.biquote_index(ser)
    if not ix: continue
    cls='crypto' if cx else 'commodity'
    rows=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(ix,G0-60)).fetchall()
    ts=[float(x[0]) for x in rows]; vs=[float(x[1]) for x in rows]
    g=[]; j=0; last=None
    for T in range(G0,G1):
        while j<len(ts) and ts[j]<=T: last=vs[j]; j+=1
        g.append(last)
    P=[0.0]
    for a,b in itertools.pairwise(g): P.append(P[-1]+((math.log(b/a))**2 if a and b else 0.0))
    lg=[math.log(x) if x else None for x in g]
    piv=[]; events=[]   # piv: (extreme_idx, logprice, +1 high/-1 low) ; events: (k_confirm, direction(+1 turn up, -1 turn down), pivot index in list)
    direction=0; ext=None; ext_i=0
    for k in range(300,len(g)):
        x=lg[k]
        if x is None: continue
        rate=math.sqrt((P[k]-P[k-300])/300)
        if rate<=0: continue
        th=1.5*rate*math.sqrt(60)
        if ext is None: ext=x; ext_i=k; continue
        if direction==0:
            if x>ext: ext=x; ext_i=k
            if x<ext-th: piv.append((ext_i,ext,1)); direction=-1; ext=x; ext_i=k; events.append((k,-1,len(piv)-1,th))
            continue
        if direction==-1:
            if x<ext: ext=x; ext_i=k
            elif x>ext+th: piv.append((ext_i,ext,-1)); direction=1; ext=x; ext_i=k; events.append((k,1,len(piv)-1,th))
        else:
            if x>ext: ext=x; ext_i=k
            elif x<ext-th: piv.append((ext_i,ext,1)); direction=-1; ext=x; ext_i=k; events.append((k,-1,len(piv)-1,th))
    pk=[p[0] for p in piv]
    opens=[w[0] for w in wl]
    for pi in range(len(piv)-1):
        (i0,_,ty0),(i1,_,ty1)=piv[pi],piv[pi+1]
        ta=G0+i0; tb=G0+i1
        wi=bisect.bisect_right(opens,ta)-1
        if wi<0: continue
        o,c,tk=wl[wi]
        if not (120<=ta-o<=780) or tb>c-15 or tb-ta<20: continue
        q,qt=quotes(tk)
        if not q: continue
        ia=bisect.bisect_right(qt,ta)-1; ib=bisect.bisect_right(qt,tb)-1
        if ia<0 or ib<=ia or ta-qt[ia]>5 or tb-qt[ib]>5: continue
        side='yes' if ty0==-1 else 'no'   # a low is followed by a rise: buy YES; a high by a fall: buy NO
        bid0,ask0=sidepx(q[ia],side)
        if not (0.25<=ask0<=0.75) or ask0-bid0>0.03: continue
        b1,_=sidepx(q[ib],side)
        n15=sum(1 for p in piv[:pi+1] if p[0]>i0-900)
        ORACLE.append((cls,n15,100*(b1-ask0-fee(ask0)-fee(max(b1,0.01))),c))
    for ei,(k,d,pi,th) in enumerate(events):
        tc=G0+k
        # regularity features from the confirmed pivots up to pi
        past=[p for p in piv[:pi+1] if p[0]>k-900]
        n15=len(past); n5=sum(1 for p in past if p[0]>k-300)
        amp=S.median([abs(b[1]-a[1])*1e4 for a,b in itertools.pairwise(past)]) if len(past)>=3 else None
        ivs=[b[0]-a[0] for a,b in itertools.pairwise(past)]
        cv=(S.pstdev(ivs)/S.mean(ivs)) if len(ivs)>=4 and S.mean(ivs)>0 else None
        fut=sum(1 for e in pk if k<e<=k+300)
        FUT.append((cls,n15,fut))
        # which market
        wi=bisect.bisect_right(opens,tc)-1
        if wi<0: continue
        o,c,tk=wl[wi]
        el=tc-o
        if not (120<=el<=780): continue
        q,qt=quotes(tk)
        if not q: continue
        i=bisect.bisect_right(qt,tc)-1
        if i<0 or tc-qt[i]>5: continue
        # next opposite turn time (cap 240 s)
        nxt=None
        for (k2,d2,_,_) in events[ei+1:ei+4]:
            if d2!=d: nxt=G0+k2; break
        z=(lg[k]-math.log(vs[bisect.bisect_left(ts,o)] if cx is None else 1))*0 if False else None
        for mode,side in (('with',('yes' if d==1 else 'no')),('against',('no' if d==1 else 'yes'))):
            bid0,ask0=sidepx(q[i],side)
            if not (0.25<=ask0<=0.75) or ask0-bid0>0.03: continue
            for dl,H in ((0.06,90),(0.10,120),(0.15,180)):
                exit_b=None; why='time'
                lim=min(tc+H,c-15)
                if lim<=tc+5: continue
                j2=i+1
                while j2<len(q) and q[j2][0]<=lim:
                    b,_=sidepx(q[j2],side)
                    if b>=ask0+dl: exit_b=b; why='tp'; break
                    j2+=1
                if exit_b is None:
                    k2=bisect.bisect_right(qt,lim)-1
                    if k2<=i: continue
                    exit_b=sidepx(q[k2],side)[0]
                p=100*(exit_b-ask0-fee(ask0)-fee(max(exit_b,0.01)))
                TRADES.append(dict(cls=cls,mode=mode,dl=dl,n15=n15,n5=n5,amp=amp,cv=cv,p=p,why=why,close=c,ask=ask0))
print('turns',len(FUT),'trades',len(TRADES))
def bucket(n): return '<=3' if n<=3 else '4-5' if n<=5 else '6-7' if n<=7 else '8+'
print('\nPERSISTENCE: after a turn, how many more swing pivots in the next 5 minutes, by how many swings in the last 15 min')
for cls in ('crypto','commodity'):
    d=collections.defaultdict(list)
    for c,n15,fu in FUT:
        if c==cls: d[bucket(n15)].append(fu)
    print(f"  {cls}: "+' | '.join(f"swings {b}: next-5-min pivots {S.mean(d[b]):.2f} (n={len(d[b])})" for b in ('<=3','4-5','6-7','8+') if d[b]))
print('\nTRADING THE TURNS: buy at the ask when a swing turn is confirmed, sell at the bid at +target or at the time limit; cents per contract after fees both ways')
for cls in ('crypto','commodity'):
    for mode in ('with','against'):
        print(f"\n== {cls}, {mode} the turn ({'YES when the spot turns up, NO when it turns down' if mode=='with' else 'the opposite side'})")
        for dl,H in ((0.06,90),(0.10,120),(0.15,180)):
            L=[t for t in TRADES if t['cls']==cls and t['mode']==mode and t['dl']==dl]
            if len(L)<100: continue
            line=f"  target +{dl:.2f}, up to {H}s: all n={len(L):5d} {S.mean(t['p'] for t in L):+.2f}c hit {100*sum(t['why']=='tp' for t in L)/len(L):3.0f}%"
            for b in ('<=3','4-5','6-7','8+'):
                X=[t for t in L if bucket(t['n15'])==b]
                if len(X)>=60:
                    cl=sorted(t['close'] for t in X); mid=cl[len(cl)//2]
                    a=[t['p'] for t in X if t['close']<mid]; z_=[t['p'] for t in X if t['close']>=mid]
                    line+=f"\n      swings {b:4}: n={len(X):5d} {S.mean(t['p'] for t in X):+.2f}c hit {100*sum(t['why']=='tp' for t in X)/len(X):3.0f}%  halves {S.mean(a):+.2f}/{S.mean(z_):+.2f}"
            print(line)
print('\nTHE CEILING: buy at the exact swing low (YES) or high (NO), found in hindsight, sell at the next swing extreme; cents per contract after fees')
for cls in ('crypto','commodity'):
    L=[o for o in ORACLE if o[0]==cls]
    if len(L)<50: continue
    print(f"  {cls}: all n={len(L)} {S.mean(o[2] for o in L):+.2f}c, share profitable {100*sum(1 for o in L if o[2]>0)/len(L):.0f}%")
    for b in ('<=3','4-5','6-7','8+'):
        X=[o for o in L if bucket(o[1])==b]
        if len(X)>=40: print(f"      swings {b:4}: n={len(X):5d} {S.mean(o[2] for o in X):+.2f}c  profitable {100*sum(1 for o in X if o[2]>0)/len(X):.0f}%")
print('DONE')
