import json, math, numpy as np
R=json.load(open(__import__('sys').argv[1]))
fee=lambda p:np.ceil(0.07*p*(1-p)*100-1e-9)/100
ll=lambda p,y:-np.mean(y*np.log(np.clip(p,1e-4,1-1e-4))+(1-y)*np.log(np.clip(1-p,1e-4,1-1e-4)))
def curve_feats(rs):
    F=[]
    for r in rs:
        p=np.clip(np.array(r['path'],float),-12,12)           # p[0]=now ... p[14]=7 min ago, typical-minute units
        area=p.mean(); area_rec=p[:5].mean(); area_old=p[5:].mean()
        v_short=(p[0]-p[4])/4.0; v_long=(p[4]-p[14])/10.0
        F.append([p[0],area,area_rec-area_old,v_short,v_long,v_short-v_long,p.max()-p[0],p[0]-p.min(),(p>0).mean(),p[0]-p[14]])
    return np.array(F)
def base_feats(rs):
    return np.array([[max(-6,min(6,r['z'])),(r['ask']-0.85)*10,r['left']/300-1,1.0 if r['side']=='yes' else 0.0,min(r['crosses'],10)/5] for r in rs])
def fit(X,y,l2=5.0,it=40):
    mu,sd=X.mean(0),X.std(0)+1e-9; Z=np.c_[np.ones(len(X)),(X-mu)/sd]; w=np.zeros(Z.shape[1])
    for _ in range(it):
        p=1/(1+np.exp(-Z@w)); g=Z.T@(p-y)+l2*np.r_[0,w[1:]]; Hs=(Z.T*(p*(1-p)))@Z+l2*np.eye(len(w)); w-=np.linalg.solve(Hs,g)
    return mu,sd,w
def pred(m,X): mu,sd,w=m; Z=np.c_[np.ones(len(X)),(X-mu)/sd]; return 1/(1+np.exp(-Z@w))
names=['level now','area (mean cushion)','area recent-old','velocity 2m','velocity 5m','acceleration','drawdown from peak','rebound from trough','share of time on side','net change 7m']
for cls in ('commodity','crypto'):
    rs=sorted([r for r in R if r['cls']==cls],key=lambda r:r['close']); B=base_feats(rs); C=curve_feats(rs)
    y=np.array([r['won'] for r in rs],float); ask=np.array([r['ask'] for r in rs]); pnl=np.array([r['pnl'] for r in rs]); left=np.array([r['left'] for r in rs]); wid=np.array([r['w'] for r in rs])
    close=np.array([r['close'] for r in rs]); mid=np.median(close); h1=close<mid; h2=~h1
    print(f'\n=== {cls}: {len(rs)} moments. held-out log-loss (lower is better); the price alone scores:')
    for lab,tr,te in (('H1->H2',h1,h2),('H2->H1',h2,h1)):
        out=[f"price {ll(ask[te],y[te]):.4f}"]
        for nm,X in (('base (z, price, time, side, crossings)',B),('base + accumulation/differential features',np.c_[B,C])):
            m=fit(X[tr],y[tr]); p=pred(m,X[te]); idx=np.where(te)[0]
            edge=p-ask[te]-fee(ask[te]); order=np.argsort(-left[te],kind='stable'); seen=set(); pk=[]
            for o in order:
                i=idx[o]
                if wid[i] in seen: continue
                if edge[o]>0.03: seen.add(wid[i]); pk.append(i)
            s=f"{len(pk)} bets {pnl[pk].mean():+.2f}/bet" if len(pk)>=20 else f'{len(pk)} bets'
            out.append(f"{nm}: {ll(p,y[te]):.4f} ({s})")
        print(f"  {lab}: "+' | '.join(out))
    X=np.c_[B,C]; m=fit(X,y); mu,sd,w=m
    nm_all=['z5','price','time left','side','crossings']+names
    top=sorted(zip(nm_all,w[1:]),key=lambda t:-abs(t[1]))[:5]; print('  strongest weights (standardized):',', '.join(f'{n} {v:+.2f}' for n,v in top))
