import json, collections, statistics as S
R=json.load(open('/tmp/ms.json'))
K='wid cls coin e side ask spread won pnl close lets zs'.split(); C={k:i for i,k in enumerate(K)}
by=collections.defaultdict(list)
for r in R: by[(r[0],r[C['side']])].append(r)
RULES={'60m D (near, approaching) and 5m F (near, pulling away)':lambda l:l[4]=='D' and l[2]=='F',
       '60m D and 5m in E or F':lambda l:l[4]=='D' and l[2] in 'EF',
       '60m D alone':lambda l:l[4]=='D',
       '60m D and 15m G and 5m F':lambda l:l[4]=='D' and l[3]=='G' and l[2]=='F',
       '60m C (far, coming back) alone':lambda l:l[4]=='C',
       'control: 5m F alone':lambda l:l[2]=='F',
       'control: 15m G alone':lambda l:l[3]=='G'}
print('PRE-SPECIFIED MULTI-SCALE RULES, one buy per market side at the first 30 s point (2:00-10:00 in) where the rule holds, ask .20-.70, spread<=.04, held to the result. Thirds = by time.')
for cls,name in (('c','CRYPTO'),('m','COMMODITIES')):
    allm=sorted({r[C['close']] for L in by.values() for r in L if r[C['cls']]==cls}); c1,c2=allm[len(allm)//3],allm[2*len(allm)//3]
    print(f'\n=== {name}')
    for rn,f in RULES.items():
        picks=[]
        for k,L in by.items():
            for r in sorted(L,key=lambda r:r[C['e']]):
                if r[C['cls']]!=cls or not (120<=r[C['e']]<=600) or r[C['spread']]>0.04 or not (0.20<=r[C['ask']]<=0.70): continue
                if f(r[C['lets']]): picks.append(r); break
        if len(picks)<60: print(f"   {rn:58} n={len(picks)} n/a"); continue
        th=[[p[C['pnl']] for p in picks if p[C['close']]<=c1],[p[C['pnl']] for p in picks if c1<p[C['close']]<=c2],[p[C['pnl']] for p in picks if p[C['close']]>c2]]
        print(f"   {rn:58} n={len(picks):4d} markets  win {100*S.mean(p[C['won']] for p in picks):3.0f}% vs price {S.mean(p[C['ask']] for p in picks):.2f}  {S.mean(p[C['pnl']] for p in picks):+.2f}/bet  by thirds {' / '.join(('%+.2f'%S.mean(t) if len(t)>=15 else 'n/a') for t in th)}  (n {len(th[0])}/{len(th[1])}/{len(th[2])})")
    if cls=='c':
        print('   crypto, rule "60m D and 5m in E or F", by coin:')
        d=collections.defaultdict(list)
        for k,L in by.items():
            for r in sorted(L,key=lambda r:r[C['e']]):
                if r[C['cls']]!=cls or not (120<=r[C['e']]<=600) or r[C['spread']]>0.04 or not (0.20<=r[C['ask']]<=0.70): continue
                l=r[C['lets']]
                if l[4]=='D' and l[2] in 'EF': d[r[C['coin']]].append(r); break
        for coin,L in sorted(d.items()): print(f"      {coin:5} n={len(L):3d} win {100*S.mean(p[C['won']] for p in L):3.0f}% vs price {S.mean(p[C['ask']] for p in L):.2f} {S.mean(p[C['pnl']] for p in L):+.2f}/bet")
print('DONE')
