import json, math, collections, statistics as S
from kalshi_moonshot.strategy import fee_per_contract as fee
R=json.load(open('/tmp/ms.json'))
K='wid cls coin e side ask spread won pnl close lets zs'.split(); C={k:i for i,k in enumerate(K)}
phi=lambda z:0.5*(1+math.erf(z/math.sqrt(2)))
by=collections.defaultdict(list)
for r in R: by[(r[0],r[C['side']])].append(r)
SC=['1m','2m','5m','15m','60m','mean of 5']
def zsel(r,si): z=r[C['zs']]; return sum(z)/5 if si==5 else z[si]
print('CHEAP-SIDE CUSHION EDGE, validated: one buy per market side at the first 30 s point (2:00-10:00 in) with ask .30-.50, spread<=.04, and edge = Phi(z) - ask - fee > threshold; held to the result; by time thirds. z uses the look-back named.')
for cls,name in (('c','CRYPTO'),('m','COMMODITIES')):
    allm=sorted({r[C['close']] for L in by.values() for r in L if r[C['cls']]==cls}); c1,c2=allm[len(allm)//3],allm[2*len(allm)//3]
    print(f'\n=== {name}')
    for si,sc in enumerate(SC):
        for th in (0.05,0.08):
            picks=[]
            for k,L in by.items():
                for r in sorted(L,key=lambda r:r[C['e']]):
                    if r[C['cls']]!=cls or not (120<=r[C['e']]<=600) or r[C['spread']]>0.04 or not (0.30<=r[C['ask']]<=0.50): continue
                    a=r[C['ask']]
                    if phi(zsel(r,si))-a-fee(a)>th: picks.append(r); break
            if len(picks)<40: print(f"   z from {sc:10} edge>{th:.2f}: n={len(picks)} n/a"); continue
            t=[[p[C['pnl']] for p in picks if p[C['close']]<=c1],[p[C['pnl']] for p in picks if c1<p[C['close']]<=c2],[p[C['pnl']] for p in picks if p[C['close']]>c2]]
            print(f"   z from {sc:10} edge>{th:.2f}: n={len(picks):4d} markets  chance {S.mean(phi(zsel(p,si)) for p in picks):.2f} price {S.mean(p[C['ask']] for p in picks):.2f} real win {100*S.mean(p[C['won']] for p in picks):3.0f}%  {S.mean(p[C['pnl']] for p in picks):+.2f}/bet  thirds {' / '.join(('%+.2f'%S.mean(x) if len(x)>=12 else 'n/a') for x in t)} (n {len(t[0])}/{len(t[1])}/{len(t[2])})")
    if cls=='c':
        print('   crypto z from 5m, edge>0.05, by coin and by letter of the 60-minute scale:')
        d=collections.defaultdict(list); d2=collections.defaultdict(list)
        for k,L in by.items():
            for r in sorted(L,key=lambda r:r[C['e']]):
                if r[C['cls']]!=cls or not (120<=r[C['e']]<=600) or r[C['spread']]>0.04 or not (0.30<=r[C['ask']]<=0.50): continue
                a=r[C['ask']]
                if phi(r[C['zs']][2])-a-fee(a)>0.05: d[r[C['coin']]].append(r); d2[r[C['lets']][4]].append(r); break
        for coin,L in sorted(d.items()): print(f"      {coin:5} n={len(L):3d} win {100*S.mean(p[C['won']] for p in L):3.0f}% vs price {S.mean(p[C['ask']] for p in L):.2f} {S.mean(p[C['pnl']] for p in L):+.2f}/bet")
        for let,L in sorted(d2.items()):
            if len(L)>=20: print(f"      60m variety {let}: n={len(L):3d} win {100*S.mean(p[C['won']] for p in L):3.0f}% vs price {S.mean(p[C['ask']] for p in L):.2f} {S.mean(p[C['pnl']] for p in L):+.2f}/bet")
print('DONE')
