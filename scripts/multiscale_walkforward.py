"""Walk-forward scan of the multi-scale variety rule space for the crypto lanes. Every crypto 15-minute window since 10/28, every 20 s from minute 2 to 14, both sides where the side's ask is 0.20-0.70 and the spread <= 0.04.
At each moment the bet's variety letter (A-G, as variants.scale_letter) is read at 1m, 2m, 5m, 15m and 60m. A rule = one scale and a letter, or two scales and a letter each. One buy per market and side at the first moment
the rule holds, held to the result, net of the taker fee, per $10 ticket. Rules are chosen on the first 60% of the markets by date (n >= 80, both train halves positive, ranked by train net) and scored on the last 40%.
The current lane rule (60m D + 5m F) is shown the same way. Correlated bets (the same window across coins) are counted as they come; the tables say n, not independent windows."""
import os, time, bisect, math, calendar, collections, itertools, statistics as S
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True); now=time.time()
t0=calendar.timegm((2026,10,28,0,0,0)); G0=int(t0-4000); G1=int(now)
FAR,SLOPE=variants.FAR,variants.SLOPE
LOOKS={'1m':60,'2m':120,'5m':300,'15m':900,'60m':3600}; SC=list(LOOKS)
COINS={'KXBTC15M':'BTC','KXETH15M':'ETH','KXSOL15M':'SOL','KXXRP15M':'XRP','KXDOGE15M':'DOGE'}
LG={}; PS={}
for ser in COINS:
    r=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(variants.crypto_index(ser),G0-60)).fetchall()
    ts=[float(x[0]) for x in r]; vs=[float(x[1]) for x in r]; g=[]; j=0; last=None
    for T in range(G0,G1):
        while j<len(ts) and ts[j]<=T: last=vs[j]; j+=1
        g.append(last)
    first=next((i for i,x in enumerate(g) if x),None)
    lg=[math.log(x) if x else (math.log(g[first]) if first is not None else 0.0) for x in g]
    P=[0.0]
    for a,b in zip(lg,lg[1:]): P.append(P[-1]+(b-a)**2)
    LG[ser]=lg; PS[ser]=P
W=st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY close_ts",(t0+4000+900,)).fetchall()
def letter(z,slope,crosses,look):
    if crosses>=max(2,round(0.8*look/60.0)): return 'G'
    if z>=FAR: return 'A' if slope>=SLOPE else 'C' if slope<=-SLOPE else 'B'
    return 'D' if slope<=-SLOPE else 'F' if slope>=SLOPE else 'E'
G=collections.defaultdict(list)   # (ticker,side) -> rows in time order
meta={}
for w in W:
    ser=w['series']
    if ser not in COINS: continue
    lg=LG[ser]; P=PS[ser]; c=float(w['close_ts']); o=c-900; K=math.log(float(w['strike']))
    ko=int(o)-G0
    if ko-3700<0 or int(c)-G0>=len(lg): continue
    q=[(float(a),float(b),float(cc)) for a,b,cc in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND bid>0 AND ask>0 ORDER BY ts",(w['ticker'],)).fetchall()]
    if len(q)<40: continue
    qt=[x[0] for x in q]
    base=ko-3700; n_=int(c)-G0-base+1
    cumc=[0]*n_; prev=None
    for j in range(n_):
        sg=lg[base+j]>K
        cumc.append(0)
        cumc[j]=(cumc[j-1] if j else 0)+(1 if (prev is not None and sg!=prev) else 0); prev=sg
    meta[w['ticker']]=(c,ser)
    for el in range(120,841,20):
        t=o+el; k=int(t)-G0
        i=bisect.bisect_right(qt,t)-1
        if i<0 or t-qt[i]>5: continue
        yb,ya=q[i][1],q[i][2]
        for side,ask,bid in (('yes',ya,yb),('no',1-yb,1-ya)):
            if not (0.20<=ask<=0.70) or ask-bid>0.04: continue
            sign=1.0 if side=='yes' else -1.0
            lets=[]; ok=True
            for sc in SC:
                L=LOOKS[sc]; sq=P[k]-P[k-L]
                if sq<=0: ok=False; break
                rate=math.sqrt(sq/L); cush=(lg[k]-K)*sign; start=(lg[k-L]-K)*sign
                z=cush/(rate*math.sqrt(c-t)); slope=(cush-start)/(rate*math.sqrt(L))
                cr=cumc[k-base]-cumc[k-L-base]
                lets.append(letter(z,slope,cr,L))
            if not ok: continue
            won=w['result']==side; cn=10/ask
            net=((1-ask)*cn if won else -10.0)-fee(ask)*cn
            G[(w['ticker'],side)].append((el,tuple(lets),net,won))
print('market-sides with a candidate moment:',len(G),flush=True)
keys=sorted(G,key=lambda k:meta[k[0]][0]); N=len(keys); cut=int(N*0.6)
train,test=keys[:cut],keys[cut:]
def first(rule,ks):
    out=[]
    for k in ks:
        for el,lets,net,won in G[k]:
            if all(lets[SC.index(sc)] in ls for sc,ls in rule):
                out.append((meta[k[0]][0],net,won)); break
    return out
def stat(L):
    if not L: return (0,0.0,0.0)
    return (len(L),sum(x[1] for x in L)/len(L),100*sum(1 for x in L if x[2])/len(L))
def halves(L):
    L=sorted(L); h=len(L)//2
    return (S.mean(x[1] for x in L[:h]) if h else 0.0, S.mean(x[1] for x in L[h:]) if len(L)-h else 0.0)
LET='ABCDEFG'
rules=[((sc,l),) for sc in SC for l in LET]+[((a,l1),(b,l2)) for a,b in itertools.combinations(SC,2) for l1 in LET for l2 in LET]
cands=[]
for r in rules:
    tr=first(r,train)
    if len(tr)<80: continue
    h1,h2=halves(tr)
    if h1>0 and h2>0: cands.append((stat(tr)[1],r,tr))
cands.sort(key=lambda x:-x[0])
print(f'\ntrain markets {len(train)} (to {time.strftime("%m-%d",time.gmtime(meta[train[-1][0]][0]))}), test markets {len(test)} (from {time.strftime("%m-%d",time.gmtime(meta[test[0][0]][0]))})')
def show(name,r):
    tr=first(r,train); te=first(r,test); a,b=stat(tr),stat(te); h=halves(te)
    print(f"  {name:34} train n{a[0]:4d} {a[2]:3.0f}% {a[1]:+5.2f} | test n{b[0]:4d} {b[2]:3.0f}% {b[1]:+5.2f} (halves {h[0]:+5.2f} {h[1]:+5.2f})")
allm=lambda ks:[(meta[k[0]][0],G[k][0][2],G[k][0][3]) for k in ks if G[k]]
a,b=stat(allm(train)),stat(allm(test))
print(f"  {'no rule (first moment in band)':34} train n{a[0]:4d} {a[2]:3.0f}% {a[1]:+5.2f} | test n{b[0]:4d} {b[2]:3.0f}% {b[1]:+5.2f}")
show('CURRENT 60m:D + 5m:F',(('60m','D'),('5m','F')))
print(f'\nrules passing the train filter: {len(cands)} of {len(rules)}; the top 10 by train net, scored on the test days:')
for sc_,r,tr in cands[:10]: show(' + '.join(f'{s_}:{l}' for s_,l in r),r)
tests=[stat(first(r,test)) for _,r,_ in cands[:10]]
print('\nacross ALL passing rules (not just the top): mean test net',round(S.mean(stat(first(r,test))[1] for _,r,_ in cands),2) if cands else None,'| share positive on test',round(100*sum(1 for _,r,_ in cands if stat(first(r,test))[1]>0)/max(1,len(cands))))
print('DONE')
