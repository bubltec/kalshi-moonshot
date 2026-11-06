"""Do rules persist? Cached rows (build_rows.py): every rule (one or two features), first moment per market and side, held to the result, net per $10 ticket. Time is cut into 6-hour blocks by close time. (1) For each
rule, the lag-1 correlation of its block means (blocks with n >= 10). (2) A switching strategy: at each block pick the rule with the best mean over the previous TWO blocks (n >= 30 there), and take what that rule makes in
the next block; compared with picking a random eligible rule and with the average of all eligible rules. (3) The same restricted to rules whose trailing mean was >= +1."""
import pickle, math, random, itertools, statistics as S, collections
from kalshi_moonshot.strategy import fee_per_contract as fee
R,mk=pickle.load(open('/tmp/rows.pkl','rb'))
R.sort(key=lambda r:(r[0],r[1]))
t_min=min(m['close'] for m in mk); BLK=lambda mid:int((mk[mid]['close']-t_min)//(6*3600))
nb=max(BLK(i) for i in range(len(mk)))+1
atoms=sorted({a for r in R for a in r[2]}); mask={a:0 for a in atoms}
for i,r in enumerate(R):
    for a in r[2]: mask[a]|=1<<i
keyof=[(r[0],r[1]) for r in R]
grp=lambda a:a.split(':')[0]
rules=[(a,) for a in atoms]+[(a,b) for a,b in itertools.combinations(atoms,2) if grp(a)!=grp(b)]
res=[]   # (rule, [list of pnl per block])
for rule in rules:
    m=-1
    for a in rule: m=mask[a] if m==-1 else m&mask[a]
    per=[[] for _ in range(nb)]; lastk=None
    while m:
        low=m&-m; i=low.bit_length()-1; m^=low
        k=keyof[i]
        if k==lastk: continue
        lastk=k; mid,side,_,ask=R[i]; cn=10/ask; won=mk[mid]['res']==side
        per[BLK(mid)].append(((1-ask)*cn if won else -10.0)-fee(ask)*cn)
    if sum(len(p) for p in per)>=150: res.append((rule,per))
print('blocks',nb,'rules with n>=150:',len(res))
cors=[]
for rule,per in res:
    pts=[(S.mean(per[b]),S.mean(per[b+1])) for b in range(nb-1) if len(per[b])>=10 and len(per[b+1])>=10]
    if len(pts)>=8:
        xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
        sx,sy=S.pstdev(xs),S.pstdev(ys)
        if sx>0 and sy>0: cors.append(sum((x-S.mean(xs))*(y-S.mean(ys)) for x,y in pts)/len(pts)/(sx*sy))
print(f"(1) lag-1 correlation of block means: mean {S.mean(cors):+.3f} over {len(cors)} rules; share positive {100*sum(1 for c in cors if c>0)/len(cors):.0f}%")
random.seed(5); picks=[]; rnd=[]; allm=[]; hot=[]
for b in range(2,nb):
    elig=[]
    for rule,per in res:
        tr=per[b-1]+per[b-2]
        if len(tr)>=30 and len(per[b])>=10: elig.append((S.mean(tr),rule,per[b]))
    if len(elig)<20: continue
    best=max(elig,key=lambda x:x[0]); picks.append(S.mean(best[2])); allm.append(S.mean(S.mean(e[2]) for e in elig)); rnd.append(S.mean(random.choice(elig)[2]))
    h=[e for e in elig if e[0]>=1.0]
    if h: hot.append(S.mean(S.mean(e[2]) for e in h))
print(f"(2) switching over {len(picks)} blocks: best-of-trailing-12h next-block mean {S.mean(picks):+.2f} | random eligible rule {S.mean(rnd):+.2f} | average of all eligible {S.mean(allm):+.2f}")
print(f"(3) rules whose trailing 12h mean was >= +1: next-block mean {S.mean(hot):+.2f} over {len(hot)} blocks")
print('DONE')
