import json, numpy as np
R=json.load(open(__import__('sys').argv[1]))
rng=np.random.default_rng(7)
def feats(rs,coins):
    X=[]
    for r in rs:
        v5,v15,v60=r['r5']/5,r['r15']/15,r['r60']/60
        a=r['ask']; p30,p60,p120=r['p30'],r['p60'],r['p120']
        d30=(a-p30) if p30 is not None else 0.0; d60=(a-p60) if p60 is not None else 0.0; d120=(a-p120) if p120 is not None else 0.0
        curv=((a-p60)-(p60-p120)) if (p60 is not None and p120 is not None) else 0.0
        X.append([v5,v15,v60,v5-v15,v15-v60,d30,d60,d120,curv,(r['orange'] or 0.0),(r['osw'] or 0),a,r['spread'],r['e']/300,1.0 if r['side']=='y' else 0.0]+[1.0 if r['coin']==c else 0.0 for c in coins])
    return np.array(X)
def pegasos(X,y,lam=0.02,epochs=40):
    n,d=X.shape; w=np.zeros(d); b=0.0; t=0
    cw={1:len(y)/(2*max((y==1).sum(),1)),-1:len(y)/(2*max((y==-1).sum(),1))}
    for _ in range(epochs):
        for i in rng.permutation(n):
            t+=1; eta=1.0/(lam*t); margin=y[i]*(X[i]@w+b)
            w*= (1-eta*lam)
            if margin<1: w+=eta*cw[y[i]]*y[i]*X[i]; b+=eta*cw[y[i]]*y[i]*0.1   # sub-gradient step on the hinge loss
    return w,b
def rff(X,D,gamma,W=None,B=None):
    if W is None: W=rng.normal(0,np.sqrt(2*gamma),(X.shape[1],D)); B=rng.uniform(0,2*np.pi,D)
    return np.sqrt(2.0/D)*np.cos(X@W+B),W,B
for cls,name in (('c','CRYPTO'),('m','COMMODITIES')):
    rs=sorted([r for r in R if r['cls']==cls],key=lambda r:r['close']); coins=sorted({r['coin'] for r in rs})
    X=feats(rs,coins); pnl=np.array([r['pnl20'] for r in rs]); y=np.where(pnl>0,1,-1); close=np.array([r['close'] for r in rs]); n=len(rs)
    cut=[np.quantile(close,1/3),np.quantile(close,2/3)]; blk=np.digitize(close,cut)   # 0,1,2 by time
    print(f"\n=== {name}: {n} early candidates (first 1:00-5:00 point per market side, ask .25-.65), label = a +0.20 scalp (else hold) made money; base {100*(y==1).mean():.0f}% / {pnl.mean():+.2f} per bet")
    for tr_blocks,te_block in (((0,),1),((0,1),2)):
        tr=np.isin(blk,tr_blocks); te=blk==te_block
        mu,sd=X[tr].mean(0),X[tr].std(0)+1e-9; Xt=(X[tr]-mu)/sd; Xe=(X[te]-mu)/sd
        print(f"  train on time block(s) {tr_blocks} ({tr.sum()}) -> test on block {te_block} ({te.sum()}); test baseline {pnl[te].mean():+.2f}/bet, hit {100*(pnl[te]>0).mean():.0f}%")
        models={}
        w,b=pegasos(Xt,y[tr]); models['linear SVM']=(Xt@w+b,Xe@w+b)
        Ft,W,B=rff(Xt,300,0.02); Fe,_,_=rff(Xe,300,0.02,W,B); w2,b2=pegasos(Ft,y[tr],lam=0.01,epochs=25); models['RBF-feature SVM']=(Ft@w2+b2,Fe@w2+b2)
        for mname,(st_,se_) in models.items():
            line=f"    {mname:16}"
            for q in (0.05,0.10,0.20,0.40):
                thr=np.quantile(st_,1-q); sel=se_>=thr; k=sel.sum()
                line+=f" | top {int(q*100):2d}%: n={k:3d} hit {100*(pnl[te][sel]>0).mean() if k else 0:3.0f}% {pnl[te][sel].mean() if k else 0:+.2f}/bet"
            print(line)
print('DONE')
