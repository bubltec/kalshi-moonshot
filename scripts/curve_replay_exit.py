import json, collections, statistics as S, math
from kalshi_moonshot.strategy import fee_per_contract as fee
R=json.load(open('/tmp/curve30.json'))
C={k:i for i,k in enumerate('wid cls coin e side ask spread z slope crosses var won pnl close'.split())}
by=collections.defaultdict(dict)
for r in R: by[(r[0],r[C['side']])][r[C['e']]]=r
for cls,name in (('m','COMMODITIES'),('c','CRYPTO')):
    bets=[]
    for (w,side),d in by.items():
        r=d.get(420)
        if r is None or r[C['cls']]!=cls or not (.76<=r[C['ask']]<=.92) or r[C['spread']]>0.03: continue
        bets.append(d)
    closes=sorted(d[420][C['close']] for d in bets); mid=closes[len(closes)//2]
    print(f"\n=== {name}: {len(bets)} favorites bought at 7:00 (ask .76-.92). Policy: sell at the first 30 s point after entry when the cushion z falls under a level (sold at the bid, fee both ways); else hold")
    print(f"  hold everything: {S.mean(d[420][C['pnl']] for d in bets):+.2f}/bet")
    for th in (1.0,0.5,0.0,-0.5,-1.0,-1.5):
        tot=[];tot1=[];tot2=[];sold=sw=sl=0
        for d in bets:
            r0=d[420]; ask=r0[C['ask']]; c=10/ask; pnl=r0[C['pnl']]; hit=None
            for e in range(450,871,30):
                r=d.get(e)
                if r is None: continue
                if r[C['z']]<th: hit=r; break
            if hit:
                bid=max(hit[C['ask']]-hit[C['spread']],0.01); pnl=c*(bid-ask)-c*fee(ask)-c*fee(bid); sold+=1
                if r0[C['won']]: sw+=1
                else: sl+=1
            tot.append(pnl); (tot1 if r0[C['close']]<mid else tot2).append(pnl)
        base=[d[420][C['pnl']] for d in bets]
        print(f"  sell when z < {th:+.1f}: {S.mean(tot):+.2f}/bet (vs hold {S.mean(base):+.2f}) H1 {S.mean(tot1):+.2f} H2 {S.mean(tot2):+.2f}; sold {sold} = {sl} that would have lost + {sw} that would have WON")
print('DONE')
