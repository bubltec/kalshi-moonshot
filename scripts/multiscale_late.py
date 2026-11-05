import json, math, collections, statistics as S
from kalshi_moonshot.strategy import fee_per_contract as fee
R=json.load(open('/tmp/ms.json'))
K='wid cls coin e side ask spread won pnl close lets zs'.split(); C={k:i for i,k in enumerate(K)}
phi=lambda z:0.5*(1+math.erf(z/math.sqrt(2)))
by=collections.defaultdict(list)
for r in R: by[(r[0],r[C['side']])].append(r)
RANGES=(('2:00-10:00 in (what I tested)',120,600),('10:00-14:00 in (the last 5 minutes)',601,840),('whole market 2:00-14:00',120,840))
RULES=(('60m D and 5m F',lambda l:l[4]=='D' and l[2]=='F',(0.20,0.70)),('60m D, 15m G, 5m F',lambda l:l[4]=='D' and l[3]=='G' and l[2]=='F',(0.20,0.70)),
       ('cheap edge, z from 5m (.30-.50, edge>.05)','e5',(0.30,0.50)),('cheap edge, z from 60m (.30-.50, edge>.05)','e60',(0.30,0.50)),
       ('cheap edge, z from 1m',"e1",(0.30,0.50)),('cheap edge, z from 2m',"e2",(0.30,0.50)))
def ok(r,rule,lo,hi):
    a=r[C['ask']]
    if not (lo<=a<=hi) or r[C['spread']]>0.04: return False
    if callable(rule): return rule(r[C['lets']])
    si={'e5':2,'e60':4,'e1':0,'e2':1}[rule]; return phi(r[C['zs']][si])-a-fee(a)>0.05
for cls,name in (('c','CRYPTO'),('m','COMMODITIES')):
    allm=sorted({r[C['close']] for L in by.values() for r in L if r[C['cls']]==cls}); c1,c2=allm[len(allm)//3],allm[2*len(allm)//3]
    print(f'\n=== {name}: one buy per market side at the first point in the range where the rule holds; held to the result; thirds by time')
    for rl,elo,ehi in RANGES:
        print(f'  -- entries {rl}')
        for rn,rule,(lo,hi) in RULES:
            picks=[]
            for k,L in by.items():
                for r in sorted(L,key=lambda r:r[C['e']]):
                    if r[C['cls']]!=cls or not (elo<=r[C['e']]<=ehi): continue
                    if ok(r,rule,lo,hi): picks.append(r); break
            if len(picks)<40: print(f"     {rn:44} n={len(picks)} n/a"); continue
            t=[[p[C['pnl']] for p in picks if p[C['close']]<=c1],[p[C['pnl']] for p in picks if c1<p[C['close']]<=c2],[p[C['pnl']] for p in picks if p[C['close']]>c2]]
            print(f"     {rn:44} n={len(picks):4d} win {100*S.mean(p[C['won']] for p in picks):3.0f}% vs price {S.mean(p[C['ask']] for p in picks):.2f} {S.mean(p[C['pnl']] for p in picks):+.2f}/bet  thirds {' / '.join(('%+.2f'%S.mean(x) if len(x)>=12 else 'n/a') for x in t)}")
    # late period: short-scale combinations 1m+2m+5m, all prices .10-.90
    rs=[r for r in R if r[C['cls']]==cls and 601<=r[C['e']]<=840 and r[C['spread']]<=0.04 and 0.10<=r[C['ask']]<=0.90]; mid=sorted(r[C['close']] for r in rs)[len(rs)//2]
    d=collections.defaultdict(list)
    for r in rs: d[r[C['lets']][:3]].append(r)
    res=[]
    for k,L in d.items():
        a=[r[C['pnl']] for r in L if r[C['close']]<mid]; b=[r[C['pnl']] for r in L if r[C['close']]>=mid]
        if len(L)>=250 and len(a)>=40 and len(b)>=40: res.append((min(S.mean(a),S.mean(b)),k,len(L),S.mean(r[C['won']] for r in L),S.mean(r[C['ask']] for r in L),S.mean(r[C['pnl']] for r in L),S.mean(a),S.mean(b)))
    res.sort(reverse=True)
    print(f"  -- LAST 5 MINUTES, every row (not first-per-market), short scales 1m+2m+5m: {len(res)} combos with n>=250; positive in both halves: {sum(1 for x in res if x[0]>0)}. Best 6:")
    for m,k,n,w,a,p,h1,h2 in res[:6]: print(f"        {k:4} n={n:5d} win {100*w:.0f}% vs price {a:.2f}  {p:+.2f}/bet  H1 {h1:+.2f} H2 {h2:+.2f}")
print('DONE')
