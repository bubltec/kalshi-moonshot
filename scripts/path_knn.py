import json, math, numpy as np
R=json.load(open(__import__('sys').argv[1]))
fee=lambda p:np.ceil(0.07*p*(1-p)*100-1e-9)/100
ll=lambda p,y:-np.mean(y*np.log(np.clip(p,1e-4,1-1e-4))+(1-y)*np.log(np.clip(1-p,1e-4,1-1e-4)))
def build(rs):
    P=np.array([r['path'] for r in rs],float)                      # cushion at 0,30,...,420 s back, in typical-minute units
    P=np.clip(P,-12,12)
    X=np.c_[P, np.array([r['left'] for r in rs])/60.0*0.5, np.array([1.0 if r['side']=='yes' else 0.0 for r in rs])*0.0]
    return X
for cls in ('commodity','crypto'):
    rs=sorted([r for r in R if r['cls']==cls],key=lambda r:r['close']); X=build(rs)
    y=np.array([r['won'] for r in rs],float); ask=np.array([r['ask'] for r in rs]); pnl=np.array([r['pnl'] for r in rs]); left=np.array([r['left'] for r in rs]); wid=np.array([r['w'] for r in rs])
    close=np.array([r['close'] for r in rs]); mid=np.median(close); h1=close<mid; h2=~h1
    print(f'\n=== {cls}: {len(rs)} moments; nearest-neighbour on the last 7 minutes of cushion path (15 points) + time left')
    for k in (30,100):
        for lab,tr,te in (('H1->H2',h1,h2),('H2->H1',h2,h1)):
            Xt,Xe=X[tr],X[te]; yt=y[tr]; p=np.zeros(len(Xe))
            for a in range(0,len(Xe),500):
                d=((Xe[a:a+500,None,:]-Xt[None,:,:])**2).sum(2); idx=np.argpartition(d,k,axis=1)[:,:k]; p[a:a+500]=yt[idx].mean(1)
            p=np.clip(p,0.02,0.98); idx=np.where(te)[0]
            edge=p-ask[te]-fee(ask[te]); order=np.argsort(-left[te],kind='stable'); seen=set(); pk=[]
            for o in order:
                i=idx[o]
                if wid[i] in seen: continue
                if edge[o]>0.03: seen.add(wid[i]); pk.append(i)
            s=f"n={len(pk):3d} win {100*y[pk].mean():.0f}% {pnl[pk].mean():+.2f}/bet" if len(pk)>=20 else f'n={len(pk)}'
            # share of moments the forecast is >=0.8 and beats the price
            hi=(p>=0.8)&(edge>0.03)
            print(f"  k={k:3d} {lab}: held-out log-loss kNN {ll(p,y[te]):.4f} vs price {ll(ask[te],y[te]):.4f}; first-moment edge>0.03 bets {s}; moments with forecast>=0.8 and edge>0.03: {hi.sum()} win {100*y[te][hi].mean() if hi.sum() else 0:.0f}%")
