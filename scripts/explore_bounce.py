"""The bounce family: an F letter (near the strike, pulling away in the bet's favor) while the bet runs AGAINST the market-wide 60-minute trend (|trend| >= 0.3%). Cached rows from build_rows.py. First moment per market and
side, held to the result, net per $10 ticket. Splits by coin, price band, minute and side; and a per-WINDOW view (all coins and sides of one 15-minute window averaged into one number) so correlated bets are counted once,
with the standard error of the window mean."""
import pickle, math, statistics as S, collections
from kalshi_moonshot.strategy import fee_per_contract as fee
R,mk=pickle.load(open('/tmp/rows.pkl','rb'))
order=sorted(range(len(mk)),key=lambda i:mk[i]['close']); rank={m:i for i,m in enumerate(order)}
blk=lambda m: min(3,rank[m]*4//len(mk))
R.sort(key=lambda r:(r[0],r[1]))
for r in R:
    nf=sum(1 for a in r[2] if (a in ('1m:F','2m:F','5m:F','15m:F','60m:F')))
    if nf>=3: r[2].append('nF:>=3')
atoms=sorted({a for r in R for a in r[2]}); mask={a:0 for a in atoms}
for i,r in enumerate(R):
    for a in r[2]: mask[a]|=1<<i
keyof=[(r[0],r[1]) for r in R]
def picks(rule):
    m=-1
    for a in rule: m=mask[a] if m==-1 else m&mask[a]
    out=[]; lastk=None
    while m:
        low=m&-m; i=low.bit_length()-1; m^=low
        k=keyof[i]
        if k==lastk: continue
        lastk=k; mid,side,_,ask=R[i]; cn=10/ask; won=mk[mid]['res']==side
        out.append((mid,((1-ask)*cn if won else -10.0)-fee(ask)*cn,won,round(mk[mid]['close']),ask))
    return out
def line(name,P):
    if len(P)<15: print(f'  {name:36} n{len(P)}'); return
    bl=[[x[1] for x in P if blk(x[0])==b] for b in range(4)]
    print(f"  {name:36} n{len(P):4d} {S.mean(x[1] for x in P):+5.2f} win {100*S.mean(1 if x[2] else 0 for x in P):3.0f}% | blocks {' '.join(f'{S.mean(b):+5.1f}({len(b)})' if b else '  -' for b in bl)}")
def window_view(P):
    w=collections.defaultdict(list)
    for x in P: w[x[3]].append(x[1])
    v=[S.mean(a) for a in w.values()]
    se=S.pstdev(v)/math.sqrt(len(v)) if len(v)>1 else float('nan')
    return len(v),S.mean(v),se
for base in (['5m:F','trend:against'],['2m:F','trend:against'],['nF:>=3','trend:against']):
    P=picks(base); print('\n'+' & '.join(base)); line('all',P)
    nw,mw,se=window_view(P); print(f"  per window: {nw} windows, mean {mw:+.2f} (standard error {se:.2f}, t = {mw/se:.1f})")
    for a in ('coin:BTC','coin:ETH','coin:SOL','coin:XRP','coin:DOGE','px:.20-.35','px:.35-.50','px:.50-.70','min:2-5','min:5-9','min:9-14','side:yes','side:no'):
        line(a,picks(base+[a]))
print('\nthe three rules together (any of them; first per market-side)')
sel={}
for base in (['5m:F','trend:against'],['2m:F','trend:against'],['nF:>=3','trend:against']):
    for x in picks(base): sel.setdefault((x[0],x[2],x[3],x[4]),x)
print('DONE')
