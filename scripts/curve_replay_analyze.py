import json, collections, statistics as S
R=json.load(open('/tmp/curve30.json'))
# (wid,cls,coin,e,side,ask,spread,z,slope,crosses,var,won,pnl,close)
C={k:i for i,k in enumerate('wid cls coin e side ask spread z slope crosses var won pnl close'.split())}
def pr(*a): print(*a)
for cls in ('m','c'):
    name={'m':'COMMODITIES','c':'CRYPTO'}[cls]
    rs=[r for r in R if r[C['cls']]==cls]; closes=sorted({r[C['close']] for r in rs}); mid=closes[len(closes)//2]
    pr(f"\n################ {name}: {len({r[0] for r in rs})} markets, {len(rs)} (market, side, 30 s point) rows")
    eb=lambda e:'0-3m' if e<180 else '3-6m' if e<360 else '6-9m' if e<540 else '9-12m' if e<720 else '12-14.5m'
    ab=lambda a:'<.50' if a<.5 else '.50-.65' if a<.65 else '.65-.76' if a<.76 else '.76-.85' if a<.85 else '.85-.92' if a<.92 else '.92+'
    pr('\nA. WIN MAP: buy this side at its ask at this point of the 15 minutes (every price, no limits). win%, $ per $10 ticket, H1/H2')
    pr('   rows are elapsed time since the market opened; columns are the price paid')
    ebs=['0-3m','3-6m','6-9m','9-12m','12-14.5m']; abs_=['.50-.65','.65-.76','.76-.85','.85-.92','.92+']
    pr('          '+''.join(f'{a:>26}' for a in abs_))
    for e in ebs:
        line=f'{e:9}'
        for a in abs_:
            L=[r for r in rs if eb(r[C['e']])==e and ab(r[C['ask']])==a]
            if len(L)<80: line+=f"{'':>26}"; continue
            h1=[r[C['pnl']] for r in L if r[C['close']]<mid]; h2=[r[C['pnl']] for r in L if r[C['close']]>=mid]
            f=lambda X:f"{S.mean(X):+.1f}" if len(X)>=30 else ' . '
            line+=f"{100*S.mean(r[C['won']] for r in L):>6.0f}% {S.mean(r[C['pnl']] for r in L):+5.2f} ({f(h1)}/{f(h2)})".rjust(26)
        pr(line)
    # B hindsight: winning side's ask over time
    pr('\nB. THE WINNING SIDE: median ask of the side that went on to win, by elapsed time (what the cheapest correct buy costs)')
    byw=collections.defaultdict(list)
    for r in rs: byw[r[0]].append(r)
    for e in (0,120,240,360,480,600,720,840):
        asks=[r[C['ask']] for r in rs if r[C['e']]==e and r[C['won']]==1]
        if asks: pr(f"   {e//60}:{e%60:02d} elapsed: winner ask median {S.median(asks):.2f}  (share of winners priced under .70: {100*sum(a<.70 for a in asks)/len(asks):.0f}%, under .80: {100*sum(a<.80 for a in asks)/len(asks):.0f}%)")
    # C: variety at 7:00 elapsed (about when the bot buys) vs at 14:30 for the favorite side
    pr('\nC. VARIETY WHEN WE BUY (7:00 into the market, favorite side priced .76-.92) vs the variety at 14:30 (30 s left), and how it ended')
    at7={ (r[0],r[C['side']]):r for r in rs if r[C['e']]==420 and .76<=r[C['ask']]<=.92}
    at14={ (r[0],r[C['side']]):r for r in rs if r[C['e']]==870}
    NAMES={'A':'far,away','B':'far,flat','C':'far,back','D':'near,toward','E':'near,flat','F':'near,away','G':'oscill.','H':'near,erode'}
    trans=collections.defaultdict(lambda:[0,0])
    for k,r in at7.items():
        r2=at14.get(k)
        if not r2: continue
        v=(r[C['var']],r2[C['var']]); trans[v][0]+=1; trans[v][1]+=r[C['won']]
    byv=collections.defaultdict(list)
    for (a,b),(n,w) in trans.items(): byv[a].append((b,n,w))
    for a in sorted(byv):
        tot=sum(n for _,n,_ in byv[a]); wins=sum(w for _,_,w in byv[a])
        if tot<30: continue
        parts=', '.join(f"->{b} {100*n/tot:.0f}% (won {100*w/n:.0f}%)" for b,n,w in sorted(byv[a],key=lambda t:-t[1])[:4] if n>=8)
        pr(f"   bought as {a} {NAMES[a]:11} n={tot:4d} won {100*wins/tot:.0f}% | at 14:30: {parts}")
    # D: for the same bets, the cushion path: z at 7:00 vs z at 14:30 for winners vs losers
    pr('\nD. THE SAME BETS: how the cushion (z) moved from 7:00 to 14:30')
    ch=collections.defaultdict(list)
    for k,r in at7.items():
        r2=at14.get(k)
        if r2: ch[r[C['won']]].append((r[C['z']],r2[C['z']]))
    for won,L in sorted(ch.items()):
        pr(f"   {'winners' if won else 'losers '}: n={len(L)}  median z at 7:00 {S.median(a for a,_ in L):+.2f}  median z at 14:30 {S.median(b for _,b in L):+.2f}")
print('DONE')
