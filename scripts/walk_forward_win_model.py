import json, math, numpy as np
R=json.load(open(__import__('sys').argv[1])); R.sort(key=lambda r:r['close_ts'])
def fee(p): return math.ceil(0.07*p*(1-p)*100-1e-9)/100
def design(rows, coins, varieties):
    X=[]
    for r in rows:
        vr=r['vol_rel']; 
        f=[r['z'],r['slope'],min(r['crosses'],10)/5,min(r['vol_ratio'],4),math.log1p(r['since_cross'])/3,(vr if vr is not None else 1.0),(0.0 if vr is not None else 1.0),
           (r['ask']-0.85)*10,r['left_s']/300-1,1.0 if r['side']=='yes' else 0.0]
        f+= [1.0 if r['coin']==c else 0.0 for c in coins]+[1.0 if r['variety']==v else 0.0 for v in varieties]
        X.append(f)
    return np.array(X)
def fit(X,y,l2=3.0,it=60):
    mu,sd=X.mean(0),X.std(0)+1e-9; Z=(X-mu)/sd; Z=np.c_[np.ones(len(Z)),Z]; w=np.zeros(Z.shape[1])
    for _ in range(it):
        p=1/(1+np.exp(-Z@w)); g=Z.T@(p-y)+l2*np.r_[0,w[1:]]; H=(Z.T*(p*(1-p)))@Z+l2*np.eye(len(w)); w-=np.linalg.solve(H,g)
    return mu,sd,w
def predict(m,X):
    mu,sd,w=m; Z=np.c_[np.ones(len(X)),(X-mu)/sd]; return 1/(1+np.exp(-Z@w))
def run(cls, cutoff_from=None):
    rows=[r for r in R if r['cls']==cls]
    coins=sorted({r['coin'] for r in rows}); varieties=sorted({r['variety'] for r in rows})
    X=design(rows,coins,varieties); y=np.array([1.0 if r['won'] else 0.0 for r in rows]); ts=np.array([r['close_ts'] for r in rows]); ask=np.array([r['ask'] for r in rows]); pnl=np.array([r['pnl'] for r in rows])
    start=ts.min()+36*3600; preds=np.full(len(rows),np.nan)
    t=start
    while t<ts.max():
        tr=ts<t-3600; te=(ts>=t)&(ts<t+6*3600)
        if tr.sum()>=300 and te.any():
            preds[te]=predict(fit(X[tr],y[tr]),X[te])
        t+=6*3600
    ok=~np.isnan(preds); idx=np.where(ok)[0]; mid=np.median(ts[ok])
    ll=lambda p,yy:-np.mean(yy*np.log(np.clip(p,1e-4,1-1e-4))+(1-yy)*np.log(np.clip(1-p,1e-4,1-1e-4)))
    print(f'\n=== {cls}: {ok.sum()} out-of-sample favorites (trained only on earlier ones)')
    print(f'  win rate {y[ok].mean():.3f}; mean ask {ask[ok].mean():.3f}; log-loss: model {ll(preds[ok],y[ok]):.4f} | price-as-probability {ll(ask[ok],y[ok]):.4f} | base rate {ll(np.full(ok.sum(),y[ok].mean()),y[ok]):.4f}')
    print(f'  buy everything: {pnl[ok].sum():+.1f} ({pnl[ok].mean():+.2f}/bet)')
    for m in (0.0,0.02,0.04,0.06,0.08):
        edge=preds-ask-np.array([fee(a) for a in ask])
        pick=ok&(edge>m)
        halves=[]
        for lab,sel in (('H1',ts<mid),('H2',ts>=mid)):
            s=pick&sel; halves.append(f'{lab} n={s.sum():4d} {pnl[s].sum():+7.1f} ({pnl[s].mean() if s.any() else 0:+.2f})')
        print(f'  buy when model - price - fee > {m:.2f}: n={pick.sum():4d} win {y[pick].mean() if pick.any() else 0:.2f} net {pnl[pick].sum():+7.1f} ({pnl[pick].mean() if pick.any() else 0:+.2f}/bet) | '+' | '.join(halves))
for c in ('crypto','commodity'): run(c)
