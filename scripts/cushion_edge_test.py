import json, math, statistics as S
R=json.load(open(__import__('sys').argv[1]))
phi=lambda z:0.5*(1+math.erf(z/math.sqrt(2)))
fee=lambda p:math.ceil(0.07*p*(1-p)*100-1e-9)/100
for cls in ('crypto','commodity'):
    rs=[r for r in R if r['cls']==cls]; rs.sort(key=lambda r:r['close']); mid=rs[len(rs)//2]['close']
    for r in rs: r['edge']=phi(r['z'])-r['ask']-fee(r['ask'])
    print(f"\n=== {cls}: {len(rs)} moments. Model chance = Phi(z); edge = Phi(z) - ask - fee")
    pb=lambda r:'<0.65' if phi(r['z'])<.65 else '0.65-0.75' if phi(r['z'])<.75 else '0.75-0.85' if phi(r['z'])<.85 else '0.85-0.93' if phi(r['z'])<.93 else '0.93+'
    print(' model chance vs real win rate:')
    for k in ('<0.65','0.65-0.75','0.75-0.85','0.85-0.93','0.93+'):
        L=[r for r in rs if pb(r)==k]
        if len(L)>=40: print(f"  model {k:10} n={len(L):5d} real win {100*S.mean(r['won'] for r in L):.0f}%  mean ask {S.mean(r['ask'] for r in L):.2f}  pnl {S.mean(r['pnl'] for r in L):+.2f}")
    print(' buy only when edge exceeds:')
    for m in (-0.05,0.0,0.03,0.06,0.10):
        L=[r for r in rs if r['edge']>m]; a=[r for r in L if r['close']<mid]; b=[r for r in L if r['close']>=mid]
        f=lambda X:f"{S.mean(x['pnl'] for x in X):+.2f}" if len(X)>=40 else ' n/a '
        print(f"  edge > {m:+.2f}: n={len(L):5d} windows {len({r['w'] for r in L}):4d} win {100*S.mean(r['won'] for r in L):.0f}%  all {f(L)}  H1 {f(a)} (n={len(a)})  H2 {f(b)} (n={len(b)})")
