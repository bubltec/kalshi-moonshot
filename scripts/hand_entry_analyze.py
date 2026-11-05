import json, math, random, statistics as S, collections
R=json.load(open('/tmp/handstudy.json'))
MON={m:i for i,m in enumerate('JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC'.split(),1)}
def key(tk):
    seg=tk.split('-')[1]; return (int(seg[0:2]),MON[seg[2:5]],int(seg[5:7]),int(seg[7:9]))
U=[r for r in R if r['kind']=='user']; C=[r for r in R if r['kind']=='ctrl']
FEATS=['level','d1','d3','d5','rng','pos','swings','since_ext','from_hi','from_lo','vol1','spread','elapsed']
hit=lambda r: 1 if r['out']>=0.10 else 0
print(f"{len(U)} of your entries, {len(C)} matched control minutes (same market and side, ask within 15c of what you paid, not within a minute of your entry)")
print(f"\n1. A +10c bid (touch) within the next 3 minutes:  YOUR entries {100*S.mean(hit(r) for r in U):.1f}%  (mean best bid move {100*S.mean(r['out'] for r in U):+.1f}c)   |  control minutes {100*S.mean(hit(r) for r in C):.1f}%  ({100*S.mean(r['out'] for r in C):+.1f}c)")
for ser in sorted({r['series'] for r in U},key=lambda s:-sum(1 for r in U if r['series']==s))[:8]:
    u=[r for r in U if r['series']==ser]; c=[r for r in C if r['series']==ser]
    if len(u)>=40 and len(c)>=100: print(f"     {ser:12} yours {100*S.mean(hit(r) for r in u):5.1f}% (n={len(u)})  controls {100*S.mean(hit(r) for r in c):5.1f}% (n={len(c)})")
cut=S.median(key(r['ticker']) for r in R)
for lab,f in (('first half of the history',lambda r:key(r['ticker'])<=cut),('second half',lambda r:key(r['ticker'])>cut)):
    u=[r for r in U if f(r)]; c=[r for r in C if f(r)]
    print(f"     {lab:26} yours {100*S.mean(hit(r) for r in u):5.1f}% (n={len(u)})  controls {100*S.mean(hit(r) for r in c):5.1f}% (n={len(c)})")
print('   by minute you entered:')
for lo,hi in ((2,3),(4,5),(6,8),(9,12)):
    u=[r for r in U if lo<=r['m']<=hi]; c=[r for r in C if lo<=r['m']<=hi]
    if len(u)>=30 and len(c)>=60: print(f"     minutes {lo}-{hi}: yours {100*S.mean(hit(r) for r in u):5.1f}% (n={len(u)})  controls {100*S.mean(hit(r) for r in c):5.1f}% (n={len(c)})")
# 2. percentile of your value among the controls of the same ticker+side
byt=collections.defaultdict(list)
for r in C: byt[(r['ticker'],r['side'])].append(r)
print('\n2. WHERE YOUR ENTRIES SIT among the other minutes of the same market (0 = lowest, 1 = highest; 0.5 = no difference), mean percentile and share above 0.5')
pr={f:[] for f in FEATS}
for r in U:
    cs=byt.get((r['ticker'],r['side']),[])
    if len(cs)<3: continue
    for f in FEATS:
        v=r[f]; lo=sum(1 for x in cs if x[f]<v); eq=sum(1 for x in cs if x[f]==v)
        pr[f].append((lo+0.5*eq)/len(cs))
for f in sorted(FEATS,key=lambda f:-abs(S.mean(pr[f])-0.5) if pr[f] else 0):
    if pr[f]: print(f"   {f:10} mean percentile {S.mean(pr[f]):.3f}   {100*sum(1 for p in pr[f] if p>0.5)/len(pr[f]):3.0f}% above 0.5   (n={len(pr[f])})   your median {S.median(r[f] for r in U):+.3f}  controls median {S.median(r[f] for r in C):+.3f}")
# 3. what precedes a +10c move (all moments)
ALL=U+C
print('\n3. WHAT PRECEDES A +10c MOVE (every moment, yours and the controls): +10c hit rate by feature fifths, and by the two halves of the history')
for f in ['d3','d1','pos','from_hi','from_lo','swings','since_ext','rng','vol1','level','elapsed']:
    vals=sorted(r[f] for r in ALL)
    qs=[vals[int(len(vals)*k/5)] for k in range(1,5)]
    def b(r):
        v=r[f]; return sum(1 for q in qs if v>q)
    line=f"   {f:10}"
    for k in range(5):
        X=[r for r in ALL if b(r)==k]
        if len(X)<80: line+="   n/a"; continue
        h1=[hit(r) for r in X if key(r['ticker'])<=cut]; h2=[hit(r) for r in X if key(r['ticker'])>cut]
        line+=f"  {100*S.mean(hit(r) for r in X):4.0f}% ({100*S.mean(h1):.0f}/{100*S.mean(h2):.0f})"
    print(line+f"    [edges {' '.join('%.2f'%q for q in qs)}]")
# 4. logistic: yours vs controls
def stdz(rows):
    mu={f:S.mean(r[f] for r in rows) for f in FEATS}; sd={f:(S.pstdev(r[f] for r in rows) or 1.0) for f in FEATS}
    return mu,sd
def fit(rows,iters=250,lr=0.5,l2=0.01):
    mu,sd=stdz(rows); w=[0.0]*(len(FEATS)+1)
    X=[[1.0]+[(r[f]-mu[f])/sd[f] for f in FEATS] for r in rows]; y=[1 if r['kind']=='user' else 0 for r in rows]
    for _ in range(iters):
        g=[0.0]*len(w)
        for x,yy in zip(X,y):
            z=sum(a*b for a,b in zip(w,x)); p=1/(1+math.exp(-max(min(z,30),-30)))
            for i in range(len(w)): g[i]+=(p-yy)*x[i]
        for i in range(len(w)): w[i]-=lr*(g[i]/len(X)+(l2*w[i] if i else 0))
    return w,mu,sd
def score(w,mu,sd,r): return sum(a*b for a,b in zip(w,[1.0]+[(r[f]-mu[f])/sd[f] for f in FEATS]))
def auc(pos,neg):
    allp=sorted([(s,1) for s in pos]+[(s,0) for s in neg]); rank=0; sr=0
    for i,(s_,l) in enumerate(allp,1):
        if l: sr+=i
    n1,n0=len(pos),len(neg); return (sr-n1*(n1+1)/2)/(n1*n0)
random.seed(1); tks=sorted({r['ticker'] for r in ALL}); random.shuffle(tks); folds=[set(tks[i::5]) for i in range(5)]
aucs=[]
for fo in folds:
    tr=[r for r in ALL if r['ticker'] not in fo]; te=[r for r in ALL if r['ticker'] in fo]
    w,mu,sd=fit(tr)
    pos=[score(w,mu,sd,r) for r in te if r['kind']=='user']; neg=[score(w,mu,sd,r) for r in te if r['kind']=='ctrl']
    if pos and neg: aucs.append(auc(pos,neg))
w,mu,sd=fit(ALL)
print(f"\n4. CAN THE CURVE FEATURES TELL YOUR ENTRY FROM OTHER MINUTES OF THE SAME PRICE? cross-validated by market: AUC {S.mean(aucs):.3f} (0.5 = no better than a coin, 1.0 = perfect)")
print('   strongest weights (standardized; positive = more typical of your entries):')
for f,wi in sorted(zip(FEATS,w[1:]),key=lambda t:-abs(t[1]))[:8]: print(f"     {f:10} {wi:+.2f}")
print('DONE')
