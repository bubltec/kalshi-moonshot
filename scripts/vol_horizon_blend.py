import json, math, itertools, numpy as np
from math import erf
R=json.load(open(__import__('sys').argv[1]))
phi=np.vectorize(lambda z:0.5*(1+erf(z/math.sqrt(2))))
fee=lambda p:np.ceil(0.07*p*(1-p)*100-1e-9)/100
H=['2m','5m','15m','1h']
def prep(cls):
    rs=sorted([r for r in R if r['cls']==cls],key=lambda r:r['close'])
    A={k:np.array([r[k] for r in rs],float) for k in ('c','left','ask','won','pnl','close','w','crosses')}
    A['r']=np.array([r['r'] for r in rs],float)   # n x 4
    return rs,A
def ll(p,y): p=np.clip(p,1e-4,1-1e-4); return -np.mean(y*np.log(p)+(1-y)*np.log(1-p))
def prob(A,w,s,idx):
    var=(A['r'][idx]**2)@np.array(w); rate=np.sqrt(var)*s
    z=A['c'][idx]/(rate*np.sqrt(A['left'][idx])); return phi(z), z
simplex=[w for w in itertools.product(np.arange(0,1.01,0.25),repeat=4) if abs(sum(w)-1)<1e-9]
scales=[0.8,1.0,1.2,1.4,1.6,1.9,2.3]
def fit(A,idx):
    best=None
    for w in simplex:
        for s in scales:
            p,_=prob(A,w,s,idx); l=ll(p,A['won'][idx])
            if best is None or l<best[0]: best=(l,w,s)
    return best
def sim(A,rs,idx,w,s,margin=0.03):
    # one bet per window at the first moment (largest left) with edge>margin and crosses<4
    p,z=prob(A,w,s,idx); ii=np.where(idx)[0]
    edge=p-A['ask'][idx]-fee(A['ask'][idx]); order=np.argsort(-A['left'][idx],kind='stable')
    seen=set(); picks=[]
    for o in order:
        i=ii[o]; wd=A['w'][i]
        if wd in seen: continue
        if edge[o]>margin and A['crosses'][i]<4: seen.add(wd); picks.append(i)
    return picks
for cls in ('commodity','crypto'):
    rs,A=prep(cls); n=len(rs); mid=np.median(A['close']); h1=A['close']<mid; h2=~h1
    print(f'\n=== {cls}: {n} moments')
    print(' log-loss by single horizon (all data; scale fit): ',end='')
    for k,name in enumerate(H):
        w=[0,0,0,0]; w[k]=1; b=min((ll(prob(A,w,s,np.ones(n,bool))[0],A['won']),s) for s in scales); print(f'{name} {b[0]:.4f} (x{b[1]})  ',end='')
    print(f"| price {ll(A['ask'],A['won']):.4f}")
    for lab,tr,te in (('fit H1 -> test H2',h1,h2),('fit H2 -> test H1',h2,h1)):
        l,w,s=fit(A,tr); p,_=prob(A,w,s,te); p5,_=prob(A,[0,1,0,0],1.0,te)
        picks=sim(A,rs,te,w,s); p5k=sim(A,rs,te,[0,1,0,0],1.0)
        f=lambda P:f"n={len(P):3d} win {100*np.mean(A['won'][P]):.0f}% {np.mean(A['pnl'][P]):+.2f}/bet" if len(P)>=20 else f'n={len(P)} n/a'
        print(f" {lab}: weights {dict(zip(H,w))} scale {s}; held-out log-loss {ll(p,A['won'][te]):.4f} vs plain 5-min {ll(p5,A['won'][te]):.4f} vs price {ll(A['ask'][te],A['won'][te]):.4f}")
        print(f"     simulated one-bet-per-window (edge>0.03): blend {f(picks)} | plain 5-min {f(p5k)}")
