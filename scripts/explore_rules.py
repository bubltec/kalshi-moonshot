"""Broad rule search with a null. Rows: every crypto 15-minute window since 10/28, every 20 s from minute 2 to 14, both sides, ask 0.20-0.70, spread <= 0.04. Features per moment: the bet's variety letter at 1m/2m/5m/15m/60m,
the 5-minute cushion z bucket, the price band, the minute bucket, the side, the coin, the New York hour block, and whether the side runs with or against the market-wide 60-minute trend. A rule is one or two of those; it buys
the first moment in a window that matches, held to the result, net of the fee, per $10 ticket. Markets are cut into four chronological blocks. A rule is SELECTED when blocks 1-3 each have n >= 30, every one of them is
positive, and together n >= 100 and the mean is >= +1.00; it is then SCORED on block 4, which the selection never saw. The same procedure is run on NULL worlds (each market's result redrawn at random with the market's own
price as the chance) to show how many rules the procedure picks, and how they score on block 4, when there is nothing to find."""
import os, time, bisect, math, calendar, collections, itertools, random, statistics as S
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
def trend60(k): return 100*sum(math.exp(LG[x][k]-LG[x][k-3600])-1 for x in COINS)/len(COINS)
def letter(z,slope,crosses,look):
    if crosses>=max(2,round(0.8*look/60.0)): return 'G'
    if z>=FAR: return 'A' if slope>=SLOPE else 'C' if slope<=-SLOPE else 'B'
    return 'D' if slope<=-SLOPE else 'F' if slope>=SLOPE else 'E'
W=st.db.execute("SELECT ticker,series,close_ts,strike,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY close_ts",(t0+4000+900,)).fetchall()
R=[]   # rows: (mkt_idx, side, atoms frozenset-ish list, ask, el)
mk=[]; mkidx={}
for w in W:
    ser=w['series']
    if ser not in COINS: continue
    lg=LG[ser]; P=PS[ser]; c=float(w['close_ts']); o=c-900; K=math.log(float(w['strike']))
    ko=int(o)-G0
    if ko-3700<0 or int(c)-G0>=len(lg): continue
    q=[(float(a),float(b),float(cc)) for a,b,cc in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND bid>0 AND ask>0 ORDER BY ts",(w['ticker'],)).fetchall()]
    if len(q)<40: continue
    qt=[x[0] for x in q]; base=ko-3700; n_=int(c)-G0-base+1
    cumc=[0]*n_; prev=None
    for j in range(n_):
        sg=lg[base+j]>K; cumc[j]=(cumc[j-1] if j else 0)+(1 if (prev is not None and sg!=prev) else 0); prev=sg
    mid=mkidx.setdefault(w['ticker'],len(mk))
    if mid==len(mk): mk.append(dict(close=c,res=w['result'],coin=COINS[ser],pyes=[]))
    for el in range(120,841,20):
        t=o+el; k=int(t)-G0
        i=bisect.bisect_right(qt,t)-1
        if i<0 or t-qt[i]>5: continue
        yb,ya=q[i][1],q[i][2]; tr=trend60(k)
        hr=time.gmtime(t-5*3600).tm_hour
        for side,ask,bid in (('yes',ya,yb),('no',1-yb,1-ya)):
            if not (0.20<=ask<=0.70) or ask-bid>0.04: continue
            sign=1.0 if side=='yes' else -1.0; lets=[]; ok=True; z5=0.0
            for sc in SC:
                L=LOOKS[sc]; sq=P[k]-P[k-L]
                if sq<=0: ok=False; break
                rate=math.sqrt(sq/L); cush=(lg[k]-K)*sign; start=(lg[k-L]-K)*sign
                z=cush/(rate*math.sqrt(c-t)); slope=(cush-start)/(rate*math.sqrt(L)); cr=cumc[k-base]-cumc[k-L-base]
                lets.append(letter(z,slope,cr,L))
                if sc=='5m': z5=z
            if not ok: continue
            atoms=[f'{sc}:{l}' for sc,l in zip(SC,lets)]
            atoms.append('z5:<0' if z5<0 else 'z5:0-1' if z5<1 else 'z5:1-2' if z5<2 else 'z5:2+')
            atoms.append('px:.20-.35' if ask<0.35 else 'px:.35-.50' if ask<0.5 else 'px:.50-.70')
            m=el/60; atoms.append('min:2-5' if m<5 else 'min:5-9' if m<9 else 'min:9-14')
            atoms.append('side:'+side); atoms.append('coin:'+COINS[ser])
            atoms.append('hr:'+('0-6' if hr<6 else '6-12' if hr<12 else '12-18' if hr<18 else '18-24'))
            if abs(tr)>=0.3: atoms.append('trend:with' if (tr>0)==(side=='yes') else 'trend:against')
            else: atoms.append('trend:flat')
            mk[mid]['pyes'].append(ask if side=='yes' else 1-ask)
            R.append((mid,side,atoms,ask))
print('rows',len(R),'markets',len(mk),flush=True)
order=sorted(range(len(mk)),key=lambda i:mk[i]['close']); rank={m:i for i,m in enumerate(order)}
blk=lambda m: min(3,rank[m]*4//len(mk))
R.sort(key=lambda r:(r[0],r[1]))
atoms_all=sorted({a for r in R for a in r[2]}); AI={a:i for i,a in enumerate(atoms_all)}
mask={a:0 for a in atoms_all}
for i,r in enumerate(R):
    for a in r[2]: mask[a]|=1<<i
keyof=[(r[0],r[1]) for r in R]
def netfor(i,res):
    mid,side,_,ask=R[i]; cn=10/ask; won=res[mid]==side
    return ((1-ask)*cn if won else -10.0)-fee(ask)*cn
def evaluate(rule_atoms,res):
    m=-1
    for a in rule_atoms: m=(mask[a] if m==-1 else m&mask[a])
    out=[[],[],[],[]]; lastk=None
    while m:
        low=m&-m; i=low.bit_length()-1; m^=low
        k=keyof[i]
        if k==lastk: continue
        lastk=k; out[blk(k[0])].append(netfor(i,res))
    return out
real={i:mk[i]['res'] for i in range(len(mk))}
# atoms groups: a pair must come from different groups
grp=lambda a: a.split(':')[0]
rules=[(a,) for a in atoms_all]+[(a,b) for a,b in itertools.combinations(atoms_all,2) if grp(a)!=grp(b)]
def run(res):
    """Every rule with n >= 100 in blocks 1-3, ranked by blocks 1-3 mean; the top ones scored on block 4 (never used for ranking)."""
    allr=[]
    for r in rules:
        bl=evaluate(r,res); tr=bl[0]+bl[1]+bl[2]
        if len(tr)>=100 and len(bl[3])>=15: allr.append((S.mean(tr),r,bl))
    allr.sort(key=lambda x:-x[0])
    print(f"rules with n>=100 on blocks 1-3 and n>=15 on block 4: {len(allr)} of {len(rules)}")
    base=[b for _,_,bl in allr for b in [S.mean(bl[3])]]
    print(f"  mean block-4 net over ALL of them {S.mean(base):+.2f}; share positive {100*sum(1 for v in base if v>0)/len(base):.0f}%")
    for top in (10,30,100):
        tm=[S.mean(bl[3]) for _,_,bl in allr[:top]]
        print(f"  the top {top} by blocks 1-3 mean: train mean {S.mean(x[0] for x in allr[:top]):+.2f} -> block-4 mean {S.mean(tm):+.2f}, positive {100*sum(1 for v in tm if v>0)/len(tm):.0f}%")
    print('\ntop 20 (train = blocks 1-3, test = block 4):')
    for m_,r,bl in allr[:20]:
        tr=bl[0]+bl[1]+bl[2]; b4=bl[3]
        print(f"  {' & '.join(r):46} train n{len(tr):4d} {m_:+5.2f} (blocks {', '.join(f'{S.mean(b):+.1f}' for b in bl[:3])}) | test n{len(b4):3d} {S.mean(b4):+5.2f}")
    return allr
run(real)
print('DONE')
