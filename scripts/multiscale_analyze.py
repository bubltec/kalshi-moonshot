import json, math, collections, statistics as S, itertools
R=json.load(open('/tmp/ms.json'))
K='wid cls coin e side ask spread won pnl close lets zs'.split(); C={k:i for i,k in enumerate(K)}
SC=['1m','2m','5m','15m','60m']
def split(rs):
    cl=sorted(r[C['close']] for r in rs); return cl[len(cl)//2]
def cell(L,mid):
    a=[r[C['pnl']] for r in L if r[C['close']]<mid]; b=[r[C['pnl']] for r in L if r[C['close']]>=mid]
    return len(L),S.mean(r[C['won']] for r in L),S.mean(r[C['ask']] for r in L),S.mean(r[C['pnl']] for r in L),(S.mean(a) if len(a)>=30 else None),(S.mean(b) if len(b)>=30 else None)
for cls,name in (('c','CRYPTO'),('m','COMMODITIES')):
    rs=[r for r in R if r[C['cls']]==cls and r[C['spread']]<=0.04 and 120<=r[C['e']]<=780]; mid=split(rs)
    print(f"\n################ {name}: {len(rs)} (market side, 30 s point) rows, all prices .10-.95 held to the result; baseline {S.mean(r[C['pnl']] for r in rs):+.2f}/bet")
    print('1. EV by the variety at ONE scale (letter; n; real win vs mean price; $/bet; H1/H2) - only cells with n>=400')
    for si,sc in enumerate(SC):
        out=[]
        for let in 'ABCDEFG':
            L=[r for r in rs if r[C['lets']][si]==let]
            if len(L)<400: continue
            n,w,a,p,h1,h2=cell(L,mid); out.append(f"{let} n={n} win {100*w:.0f}%/ask {a:.2f} {p:+.2f} ({'%+.2f'%h1 if h1 is not None else ' . '}/{'%+.2f'%h2 if h2 is not None else ' . '})")
        print(f"   {sc:>3}: "+' | '.join(out))
    def tuples(idx,label,minn=250,top=6):
        d=collections.defaultdict(list)
        for r in rs: d[''.join(r[C['lets']][i] for i in idx)].append(r)
        res=[]
        for k,L in d.items():
            if len(L)<minn: continue
            n,w,a,p,h1,h2=cell(L,mid)
            if h1 is None or h2 is None: continue
            res.append((min(h1,h2),k,n,w,a,p,h1,h2))
        res.sort(reverse=True); tested=len(res); both=sum(1 for x in res if x[0]>0)
        print(f"   {label}: {tested} combos with n>={minn}; positive in both halves: {both}. Best by the worse half:")
        for m,k,n,w,a,p,h1,h2 in res[:top]: print(f"        {k:6} n={n:4d} win {100*w:.0f}% vs price {a:.2f}  {p:+.2f}/bet  H1 {h1:+.2f} H2 {h2:+.2f}")
    print('2. COMBINATIONS OF SCALES (letters in scale order)')
    tuples((2,3),'5m+15m'); tuples((2,4),'5m+60m'); tuples((0,2),'1m+5m'); tuples((1,3),'2m+15m'); tuples((2,3,4),'5m+15m+60m',minn=150); tuples((0,1,2,3,4),'all five scales',minn=100,top=8)
    # 3. change over the last 2 minutes
    prev={(r[C['wid']],r[C['side']],r[C['e']]):r for r in R if r[C['cls']]==cls}
    d=collections.defaultdict(list)
    for r in rs:
        p=prev.get((r[C['wid']],r[C['side']],r[C['e']]-120))
        if p: d[p[C['lets']][2]+'>'+r[C['lets']][2]].append(r)
    res=[]
    for k,L in d.items():
        if len(L)<250: continue
        n,w,a,p_,h1,h2=cell(L,mid)
        if h1 is None or h2 is None: continue
        res.append((min(h1,h2),k,n,w,a,p_,h1,h2))
    res.sort(reverse=True)
    print(f"3. CHANGE OF THE 5-MINUTE VARIETY OVER THE LAST 2 MINUTES (then>now): {len(res)} transitions with n>=250; positive in both halves: {sum(1 for x in res if x[0]>0)}. Best:")
    for m,k,n,w,a,p_,h1,h2 in res[:6]: print(f"        {k:5} n={n:4d} win {100*w:.0f}% vs price {a:.2f}  {p_:+.2f}/bet  H1 {h1:+.2f} H2 {h2:+.2f}")
print('DONE')
