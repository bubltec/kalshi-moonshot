"""The F theme (near the strike and pulling away in the bet's favor), on the cached moment rows (/tmp/rows.pkl from build_rows.py). One buy per market and side at the first moment a rule holds, held to the result,
net of the fee, per $10 ticket, by four chronological blocks of markets. Block 4 is the newest days. Also reports distinct windows (one 15-minute window is up to five coins that move together)."""
import pickle, statistics as S, itertools, collections
from kalshi_moonshot.strategy import fee_per_contract as fee
R,mk=pickle.load(open('/tmp/rows.pkl','rb'))
order=sorted(range(len(mk)),key=lambda i:mk[i]['close']); rank={m:i for i,m in enumerate(order)}
blk=lambda m: min(3,rank[m]*4//len(mk))
R.sort(key=lambda r:(r[0],r[1]))
for r in R:
    nf=sum(1 for a in r[2] if a.endswith(':F') and a[:2] in ('1m','2m','5m') or a in ('15m:F','60m:F'))
    for k in (2,3,4): 
        if nf>=k: r[2].append(f'nF:>={k}')
atoms=sorted({a for r in R for a in r[2]}); mask={a:0 for a in atoms}
for i,r in enumerate(R):
    for a in r[2]: mask[a]|=1<<i
keyof=[(r[0],r[1]) for r in R]
def net(i):
    mid,side,_,ask=R[i]; cn=10/ask; won=mk[mid]['res']==side
    return ((1-ask)*cn if won else -10.0)-fee(ask)*cn, won
def evaluate(rule):
    m=-1
    for a in rule: m=mask[a] if m==-1 else m&mask[a]
    out=[[],[],[],[]]; lastk=None; wins=set()
    while m:
        low=m&-m; i=low.bit_length()-1; m^=low
        k=keyof[i]
        if k==lastk: continue
        lastk=k; n_,w=net(i); out[blk(k[0])].append(n_); wins.add(round(mk[k[0]]['close']))
    return out,len(wins)
def show(rule,name=None):
    bl,nw=evaluate(rule); al=[x for b in bl for x in b]
    if not al: print(f'  {name or " & ".join(rule):38} no rows'); return
    cells=' '.join(f'{S.mean(b):+5.1f}({len(b):3d})' if b else '   - (  0)' for b in bl)
    print(f"  {name or ' & '.join(rule):38} n{len(al):5d} win-windows {nw:4d} all {S.mean(al):+5.2f} | blocks {cells}")
print('block means (n) per $10 ticket; block 4 = newest')
show(['px:.20-.35'],'BASELINE first moment .20-.35'); show(['px:.35-.50'],'BASELINE .35-.50'); show(['px:.50-.70'],'BASELINE .50-.70')
print('\nF at one scale'); [show([f'{s}:F']) for s in ('1m','2m','5m','15m','60m')]
print('\nF at several scales'); [show(r) for r in (['15m:F','5m:F'],['15m:F','2m:F'],['60m:F','15m:F'],['60m:F','5m:F'],['15m:F','5m:F','60m:F'],['nF:>=2'],['nF:>=3'],['nF:>=4'])]
print('\n15m:F by price, minute, side, trend, coin')
for a in ('px:.20-.35','px:.35-.50','px:.50-.70','min:2-5','min:5-9','min:9-14','side:yes','side:no','trend:with','trend:against','trend:flat','coin:BTC','coin:ETH','coin:SOL','coin:XRP','coin:DOGE','hr:0-6','hr:6-12','hr:12-18','hr:18-24'): show(['15m:F',a])
print('\nnF>=3 by price, minute, trend'); [show(['nF:>=3',a]) for a in ('px:.20-.35','px:.35-.50','px:.50-.70','min:2-5','min:5-9','min:9-14','trend:with','trend:against','trend:flat')]
print('\nthe current lane rule and the "5m:F against the hour" rule'); show(['60m:D','5m:F']); show(['5m:F','trend:against']); show(['2m:F','trend:against'])
print('DONE')
