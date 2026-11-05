import json, math, random, statistics as S, collections, datetime, itertools
MON={m:i for i,m in enumerate('JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC'.split(),1)}
def close_utc(tk):
    seg=tk.split('-')[1]; yy,mo,dd,hh,mi=int(seg[0:2]),MON[seg[2:5]],int(seg[5:7]),int(seg[7:9]),int(seg[9:11])
    est=(mo==11 and dd>=1 and (hh>=2 or dd>1)) or mo==12
    return datetime.datetime(2000+yy,mo,dd,hh,mi,tzinfo=datetime.timezone.utc).timestamp()+(5 if est else 4)*3600
R=json.load(open('/tmp/handstudy.json')); B=json.load(open('/tmp/spotbars.json'))
def zig(vals,th):
    piv=[]; d=0; ext=vals[0]; ei=0
    for i,x in enumerate(vals):
        if d>=0 and x>ext: ext=x; ei=i
        elif d<=0 and x<ext: ext=x; ei=i
        if d>=0 and ext-x>=th and i>ei: piv.append((ei,1)); d=-1; ext=x; ei=i
        elif d<=0 and x-ext>=th and i>ei: piv.append((ei,-1)); d=1; ext=x; ei=i
        elif d==0 and abs(x-vals[0])>=th: d=1 if x>vals[0] else -1; ext=x; ei=i
    return piv
def sfeats(r):
    bars=B.get(r['ticker'])
    if not bars: return None
    c=close_utc(r['ticker']); o=c-900; side=1.0 if r['side']=='yes' else -1.0
    at=o+r['m']*60
    prior=[b for b in bars if b[0]+60<=at]          # completed minute bars before the entry minute
    pre=[b for b in bars if b[0]+60<=o]
    if len(prior)<20 or len(pre)<5: return None
    kb=pre[-1]; K=(kb[1]+kb[2]+kb[3]+kb[4])/4.0
    cl=[b[4] for b in prior]; last=cl[-1]
    rets=[math.log(b/a) for a,b in itertools.pairwise(cl[-61:])]
    sig=S.pstdev(rets) if len(rets)>10 else None
    if not sig or sig<=0: return None
    left=(c-at)/60.0
    cush=math.log(last/K)*side
    f={'cush_z':cush/(sig*math.sqrt(max(left,0.5)))}
    for n in (1,3,5,15):
        f[f'r{n}']=side*math.log(last/cl[-1-n])/(sig*math.sqrt(n))
    w5=cl[-5:]; w15=cl[-15:]
    f['pos5']=((last-min(w5))/(max(w5)-min(w5)) if max(w5)>min(w5) else 0.5) if side>0 else ((max(w5)-last)/(max(w5)-min(w5)) if max(w5)>min(w5) else 0.5)
    f['pos15']=((last-min(w15))/(max(w15)-min(w15)) if max(w15)>min(w15) else 0.5) if side>0 else ((max(w15)-last)/(max(w15)-min(w15)) if max(w15)>min(w15) else 0.5)
    f['range15']=math.log(max(w15)/min(w15))/(sig*math.sqrt(15))
    lg=[math.log(x) for x in cl[-16:]]
    pv=zig(lg,1.0*sig)
    f['swings']=len(pv)
    f['since_pivot']=(len(lg)-1-pv[-1][0]) if pv else 15
    f['last_pivot_high']=1.0 if (pv and pv[-1][1]==1) else (0.0 if pv else 0.5)    # 1 = the last pivot was a high (spot is coming down off a peak)
    if len(pv)>=3:
        iv=[b[0]-a[0] for a,b in itertools.pairwise(pv)]; f['period']=S.mean(iv); f['regular']=1.0-min(1.0,(S.pstdev(iv)/S.mean(iv)) if S.mean(iv)>0 else 1.0)
        f['phase']=f['since_pivot']/S.mean(iv)
    else: f['period']=0.0; f['regular']=0.0; f['phase']=0.0
    sg=[1 if x>K else 0 for x in [b[4] for b in bars if o<=b[0]<at]]
    f['cross_so_far']=sum(1 for a,b in itertools.pairwise(sg) if a!=b) if len(sg)>1 else 0
    f['frac_fav']=(sum(sg)/len(sg) if side>0 else 1-sum(sg)/len(sg)) if sg else 0.5
    f['spike3']=f['r3']       # side-signed: negative = the side's spot just fell (the contract of the OPPOSITE side spiked)
    return f
rows=[]
for r in R:
    if r['series'] not in ('KXBTC15M','KXETH15M','KXSOL15M','KXXRP15M','KXDOGE15M'): continue
    f=sfeats(r)
    if f: rows.append(dict(r,**f))
U=[r for r in rows if r['kind']=='user']; C=[r for r in rows if r['kind']=='ctrl']
SF=['cush_z','r1','r3','r5','r15','pos5','pos15','range15','swings','since_pivot','last_pivot_high','regular','phase','cross_so_far','frac_fav']
hit=lambda r:1 if r['out']>=0.10 else 0
key=lambda tk:close_utc(tk)
cut=S.median(key(r['ticker']) for r in rows)
print(f"{len(U)} of your crypto entries with spot data, {len(C)} matched control minutes")
print(f"+10c touch within 3 minutes: yours {100*S.mean(hit(r) for r in U):.1f}%  controls {100*S.mean(hit(r) for r in C):.1f}%")
byt=collections.defaultdict(list)
for r in C: byt[(r['ticker'],r['side'])].append(r)
print('\nSPOT FEATURES: where your entries sit among the other minutes of the same market (percentile; 0.5 = no difference)')
pr={f:[] for f in SF}
for r in U:
    cs=byt.get((r['ticker'],r['side']),[])
    if len(cs)<3: continue
    for f in SF:
        v=r[f]; lo=sum(1 for x in cs if x[f]<v); eq=sum(1 for x in cs if x[f]==v); pr[f].append((lo+0.5*eq)/len(cs))
for f in sorted(SF,key=lambda f:-abs(S.mean(pr[f])-0.5) if pr[f] else 0):
    if pr[f]: print(f"   {f:15} {S.mean(pr[f]):.3f}  ({100*sum(1 for p in pr[f] if p>0.5):.0f}/{len(pr[f])} above)   yours median {S.median(r[f] for r in U):+.2f}  controls {S.median(r[f] for r in C):+.2f}")
ALL=U+C
print('\nWHAT PRECEDES A +10c MOVE (all crypto moments): hit rate by fifths of each spot feature (first half / second half of the history)')
for f in ['cush_z','r3','r5','r15','pos5','range15','swings','since_pivot','regular','phase','cross_so_far','frac_fav']:
    vals=sorted(r[f] for r in ALL); qs=[vals[int(len(vals)*k/5)] for k in range(1,5)]
    line=f"   {f:13}"
    for k in range(5):
        X=[r for r in ALL if sum(1 for q in qs if r[f]>q)==k]
        if len(X)<80: line+="   n/a"; continue
        h1=[hit(r) for r in X if key(r['ticker'])<=cut]; h2=[hit(r) for r in X if key(r['ticker'])>cut]
        line+=f"  {100*S.mean(hit(r) for r in X):3.0f}% ({100*S.mean(h1) if h1 else float('nan'):.0f}/{100*S.mean(h2) if h2 else float('nan'):.0f})"
    print(line+f"   [{' '.join('%.2f'%q for q in qs)}]")
print('\nTHE WAVE HYPOTHESIS: the spot has been oscillating regularly (3+ swings of 1 typical minute, regularity >= 0.6) and the entry is near a peak of the side opposite to the bet:')
W=[r for r in ALL if r['swings']>=3 and r['regular']>=0.6]
print(f"   regular-wave moments: n={len(W)} hit rate {100*S.mean(hit(r) for r in W):.0f}% (not regular: {100*S.mean(hit(r) for r in ALL if r not in W):.0f}%)")
for lab,fn in (('...and the last pivot was a HIGH of the spot',lambda r:r['last_pivot_high']==1.0),('...and the last pivot was a LOW',lambda r:r['last_pivot_high']==0.0)):
    X=[r for r in W if fn(r)]
    if len(X)>=40: print(f"     {lab}: n={len(X)} hit {100*S.mean(hit(r) for r in X):.0f}%   yours {100*S.mean(hit(r) for r in X if r['kind']=='user'):.0f}% (n={sum(1 for r in X if r['kind']=='user')})")

CF=['level','d1','d3','d5','rng','pos','since_ext','from_hi','from_lo','vol1','spread','elapsed']
def fit(rows,feats,iters=200,lr=0.5,l2=0.01):
    mu={f:S.mean(r[f] for r in rows) for f in feats}; sd={f:(S.pstdev(r[f] for r in rows) or 1.0) for f in feats}
    w=[0.0]*(len(feats)+1); X=[[1.0]+[(r[f]-mu[f])/sd[f] for f in feats] for r in rows]; y=[1 if r['kind']=='user' else 0 for r in rows]
    for _ in range(iters):
        g=[0.0]*len(w)
        for x,yy in zip(X,y):
            z=sum(a*b for a,b in zip(w,x)); p=1/(1+math.exp(-max(min(z,30),-30)))
            for i in range(len(w)): g[i]+=(p-yy)*x[i]
        for i in range(len(w)): w[i]-=lr*(g[i]/len(X)+(l2*w[i] if i else 0))
    return w,mu,sd
def score(m,feats,r):
    w,mu,sd=m; return sum(a*b for a,b in zip(w,[1.0]+[(r[f]-mu[f])/sd[f] for f in feats]))
def auc(pos,neg):
    allp=sorted([(s_,1) for s_ in pos]+[(s_,0) for s_ in neg]); sr=sum(i for i,(s_,l) in enumerate(allp,1) if l); n1,n0=len(pos),len(neg)
    return (sr-n1*(n1+1)/2)/(n1*n0)
random.seed(2); tks=sorted({r['ticker'] for r in ALL}); random.shuffle(tks); folds=[set(tks[i::5]) for i in range(5)]
print('\nCAN THE FEATURES TELL YOUR ENTRY FROM OTHER MINUTES OF THE SAME PRICE? cross-validated by market (AUC; 0.5 = chance)')
for lab,feats in (('contract price features only',CF),('spot features only',SF),('both',CF+SF)):
    a=[]
    for fo in folds:
        tr=[r for r in ALL if r['ticker'] not in fo]; te=[r for r in ALL if r['ticker'] in fo]
        m=fit(tr,feats); pos=[score(m,feats,r) for r in te if r['kind']=='user']; neg=[score(m,feats,r) for r in te if r['kind']=='ctrl']
        if pos and neg: a.append(auc(pos,neg))
    print(f"   {lab:30} AUC {S.mean(a):.3f}")
m=fit(ALL,CF+SF)
print('   strongest weights with both (positive = more typical of your entries):')
for f,wi in sorted(zip(CF+SF,m[0][1:]),key=lambda t_:-abs(t_[1]))[:9]: print(f"     {f:15} {wi:+.2f}")
print('DONE')
