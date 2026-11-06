# Appended to scripts/hand_entry_study.py's body (it leaves `rows` in memory). Each feature is z-scored within its own series, so every market is read in its own variance. Can the curve features tell your entry
# from other minutes of the same market (label 1 = your entry, 0 = a matched control minute), (a) within a series (5-fold by market), (b) trained on BTC alone and tested on each other series, (c) trained on every
# other series and tested on this one ("shared")? AUC 0.5 = chance. Then by entry minute for BTC.
import random
FE=['level','d1','d3','d5','rng','pos','swings','since_ext','from_hi','from_lo','vol1','spread','elapsed']
bs=collections.defaultdict(list)
for r in rows: bs[r['series']].append(r)
for ser,L in bs.items():
    st_={f:(S.mean(x[f] for x in L),S.pstdev([x[f] for x in L]) or 1.0) for f in FE}
    for x in L: x['z']=[(x[f]-st_[f][0])/st_[f][1] for f in FE]
def fit(L,iters=250,lr=0.3,l2=0.01):
    w=[0.0]*(len(FE)+1); n=len(L)
    for _ in range(iters):
        g=[0.0]*len(w)
        for x in L:
            z=w[0]+sum(a*b for a,b in zip(w[1:],x['z'])); p=1/(1+math.exp(-max(-30,min(30,z)))); e=p-(1 if x['kind']=='user' else 0)
            g[0]+=e
            for j,v in enumerate(x['z']): g[j+1]+=e*v
        w=[a-lr*(b/n+l2*a) for a,b in zip(w,g)]
    return w
def score(w,x): return w[0]+sum(a*b for a,b in zip(w[1:],x['z']))
def auc(w,L):
    P=[score(w,x) for x in L if x['kind']=='user']; N=[score(w,x) for x in L if x['kind']=='ctrl']
    if len(P)<10 or len(N)<30: return None
    return sum((p>q)+0.5*(p==q) for p in P for q in N)/(len(P)*len(N))
fmt=lambda a: '  n/a ' if a is None else f'{a:6.3f}'
print('\nrows by series (your entries / controls):')
big=[s_ for s_,L in sorted(bs.items(),key=lambda kv:-len(kv[1])) if sum(1 for x in L if x['kind']=='user')>=40]
for s_ in big: print(f"  {s_:12} {sum(1 for x in bs[s_] if x['kind']=='user'):4d} / {sum(1 for x in bs[s_] if x['kind']=='ctrl'):5d}")
random.seed(3)
def cv(L):
    tks=sorted({x['ticker'] for x in L}); random.shuffle(tks); folds=[set(tks[i::5]) for i in range(5)]; out=[]
    for fo in folds:
        a=fit([x for x in L if x['ticker'] not in fo]); r=auc(a,[x for x in L if x['ticker'] in fo])
        if r is not None: out.append(r)
    return S.mean(out) if out else None
btc=fit(bs['KXBTC15M']) if 'KXBTC15M' in bs else None
print(f"\n{'series':12} {'within (CV)':>12} {'train BTC':>10} {'train others':>13}")
for s_ in big:
    L=bs[s_]; oth=[x for k,v in bs.items() if k!=s_ for x in v]
    print(f"{s_:12} {fmt(cv(L)):>12} {fmt(auc(btc,L)) if (btc and s_!='KXBTC15M') else '     -':>10} {fmt(auc(fit(oth),L)):>13}")
print('\nBTC within-series, by the minute you entered (CV by market, then pooled model on that minute range):')
L=bs.get('KXBTC15M',[])
for lo,hi in ((2,4),(5,8),(9,12)):
    sub=[x for x in L if lo<=x['m']<=hi]
    if sum(1 for x in sub if x['kind']=='user')>=30: print(f"  minutes {lo}-{hi}: AUC {fmt(cv(sub))}  (n yours {sum(1 for x in sub if x['kind']=='user')})")
print('\nBTC model weights (z-scored features, positive = more common at your entries):')
if btc:
    for f,wt in sorted(zip(FE,btc[1:]),key=lambda kv:-abs(kv[1])): print(f'  {f:10} {wt:+.2f}')
print('DONE')
