"""Without the bid-ask spread (how you buy, not what the curve does), on the rebuilt hand-entry dataset (/tmp/handstudy.json from hand_entry_study.py), every feature z-scored within its own series.
A. Can the curve still tell your entry from other minutes of the same market? Within a series (CV by market), trained on BTC alone, trained on all other series.
B. Does the curve predict a good moment? Label = the side's bid touches +10c within the next 3 minutes (every row, yours and controls). Same three ways, plus the hit rate of the top third of scores against all.
C. Among YOUR entries only: does the curve predict which ones won (net > 0)? Same three ways."""
import json, math, random, statistics as S, collections
R=json.load(open('/tmp/handstudy.json'))
FE=['level','d1','d3','d5','rng','pos','swings','since_ext','from_hi','from_lo','vol1','elapsed']   # spread left out
bs=collections.defaultdict(list)
for r in R: bs[r['series']].append(r)
for ser,L in bs.items():
    st_={f:(S.mean(x[f] for x in L),S.pstdev([x[f] for x in L]) or 1.0) for f in FE}
    for x in L: x['z']=[(x[f]-st_[f][0])/st_[f][1] for f in FE]
def fit(L,y,iters=250,lr=0.3,l2=0.01):
    w=[0.0]*(len(FE)+1); n=len(L)
    for _ in range(iters):
        g=[0.0]*len(w)
        for x in L:
            z=w[0]+sum(a*b for a,b in zip(w[1:],x['z'])); p=1/(1+math.exp(-max(-30,min(30,z)))); e=p-y(x)
            g[0]+=e
            for j,v in enumerate(x['z']): g[j+1]+=e*v
        w=[a-lr*(b/n+l2*a) for a,b in zip(w,g)]
    return w
sc=lambda w,x: w[0]+sum(a*b for a,b in zip(w[1:],x['z']))
def auc(w,L,y):
    P=[sc(w,x) for x in L if y(x)==1]; N=[sc(w,x) for x in L if y(x)==0]
    if len(P)<15 or len(N)<30: return None
    return sum((p>q)+0.5*(p==q) for p in P for q in N)/(len(P)*len(N))
fmt=lambda a: '  n/a ' if a is None else f'{a:6.3f}'
random.seed(3)
def cv(L,y):
    tks=sorted({x['ticker'] for x in L}); random.shuffle(tks); folds=[set(tks[i::5]) for i in range(5)]; out=[]
    for fo in folds:
        a=fit([x for x in L if x['ticker'] not in fo],y); r=auc(a,[x for x in L if x['ticker'] in fo],y)
        if r is not None: out.append(r)
    return S.mean(out) if out else None
big=[s_ for s_,L in sorted(bs.items(),key=lambda kv:-len(kv[1])) if sum(1 for x in L if x['kind']=='user')>=40]
def table(title,y,rows_of,lift=False):
    print('\n'+title); print(f"{'series':12} {'within (CV)':>12} {'train BTC':>10} {'train others':>13}"+('  top-third hit vs all' if lift else ''))
    btc=fit(rows_of(bs['KXBTC15M']),y)
    for s_ in big:
        L=rows_of(bs[s_]); oth=[x for k,v in bs.items() if k!=s_ for x in rows_of(v)]
        wo=fit(oth,y); line=f"{s_:12} {fmt(cv(L,y)):>12} {fmt(auc(btc,L,y)) if s_!='KXBTC15M' else '     -':>10} {fmt(auc(wo,L,y)):>13}"
        if lift and len(L)>60:
            w_=btc if s_!='KXBTC15M' else wo; ss=sorted(L,key=lambda x:-sc(w_,x)); top=ss[:len(ss)//3]
            line+=f"  {100*S.mean(y(x) for x in top):4.0f}% vs {100*S.mean(y(x) for x in L):4.0f}%"
        print(line)
user=lambda L:[x for x in L if x['kind']=='user']
allr=lambda L:L
table('A. your entry (1) vs other minutes of the same market (0), no spread',lambda x:1 if x['kind']=='user' else 0,allr)
table('B. a +10c bid touch within 3 minutes, all moments (top third = the BTC/others model\'s highest scores)',lambda x:1 if x['out']>=0.10 else 0,allr,lift=True)
table('C. your entries only: did the trip finish in profit?',lambda x:1 if (x['pnl'] or 0)>0 else 0,user,lift=True)
print('DONE')
