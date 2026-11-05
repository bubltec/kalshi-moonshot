import json, math, numpy as np
R=json.load(open(__import__('sys').argv[1]))
fee=lambda p:np.ceil(0.07*p*(1-p)*100-1e-9)/100
def feats(rs):
    X=[]
    for r in rs:
        c,L=r['c'],r['left']; z=[c/(x*math.sqrt(L)) for x in r['r']]
        z=[max(-6,min(6,v)) for v in z]
        X.append(z+[math.log(r['r'][0]/r['r'][3]),math.log(r['r'][1]/r['r'][3]),min(r['crosses'],10)/5,(r['ask']-0.85)*10,L/300-1,1.0 if r['side']=='yes' else 0.0])
    return np.array(X)
def fit(X,y,l2=5.0,it=40):
    mu,sd=X.mean(0),X.std(0)+1e-9; Z=np.c_[np.ones(len(X)),(X-mu)/sd]; w=np.zeros(Z.shape[1])
    for _ in range(it):
        p=1/(1+np.exp(-Z@w)); g=Z.T@(p-y)+l2*np.r_[0,w[1:]]; Hs=(Z.T*(p*(1-p)))@Z+l2*np.eye(len(w)); w-=np.linalg.solve(Hs,g)
    return mu,sd,w
def pred(m,X): mu,sd,w=m; Z=np.c_[np.ones(len(X)),(X-mu)/sd]; return 1/(1+np.exp(-Z@w))
ll=lambda p,y:-np.mean(y*np.log(np.clip(p,1e-4,1-1e-4))+(1-y)*np.log(np.clip(1-p,1e-4,1-1e-4)))
for cls in ('commodity','crypto'):
    rs=sorted([r for r in R if r['cls']==cls],key=lambda r:r['close']); X=feats(rs); y=np.array([r['won'] for r in rs],float)
    ask=np.array([r['ask'] for r in rs]); pnl=np.array([r['pnl'] for r in rs]); left=np.array([r['left'] for r in rs]); wid=np.array([r['w'] for r in rs]); cr=np.array([r['crosses'] for r in rs])
    close=np.array([r['close'] for r in rs]); mid=np.median(close); h1=close<mid; h2=~h1
    print(f'\n=== {cls}: logistic on the four-horizon cushions, vol ratios, crossings, price, time left, side')
    for lab,tr,te in (('fit H1 -> test H2',h1,h2),('fit H2 -> test H1',h2,h1)):
        m=fit(X[tr],y[tr]); p=pred(m,X[te]); idx=np.where(te)[0]
        edge=p-ask[te]-fee(ask[te]); order=np.argsort(-left[te],kind='stable'); seen=set(); pk=[]
        for o in order:
            i=idx[o]
            if wid[i] in seen: continue
            if edge[o]>0.03: seen.add(wid[i]); pk.append(i)
        s=f"n={len(pk):3d} win {100*y[pk].mean():.0f}% {pnl[pk].mean():+.2f}/bet" if len(pk)>=20 else f'n={len(pk)}'
        print(f" {lab}: held-out log-loss model {ll(p,y[te]):.4f} vs price {ll(ask[te],y[te]):.4f}; one bet per window at first edge>0.03: {s}")
