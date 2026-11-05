import os, calendar, bisect, collections, statistics as S, math
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
t0=calendar.timegm((2026,11,3,0,0,0))
W=st.db.execute("SELECT ticker,series,close_ts FROM windows WHERE close_ts>? AND result IN ('yes','no') ORDER BY close_ts",(t0,)).fetchall()
HOLDS=(5,10,30,60,120)
res=collections.defaultdict(list)   # (cls, hold, momentum bucket) -> list of pnl cents per contract
allp=collections.defaultdict(list)
n=0
for w in W:
    ser=w['series']
    if ser in variants.RECORD_ONLY: continue
    cx=variants.crypto_index(ser); ix=cx or variants.biquote_index(ser)
    if not ix: continue
    cls='crypto' if cx else 'commodity'
    close=float(w['close_ts']); opened=close-900
    q=[(float(a),float(b),float(c)) for a,b,c in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",(w['ticker'],opened,close)).fetchall()]
    if len(q)<40: continue
    qt=[x[0] for x in q]; n+=1
    def side_q(i,side): return (q[i][1],q[i][2]) if side=='yes' else (1-q[i][2],1-q[i][1])
    for e in range(120,840,10):
        t=opened+e; i=bisect.bisect_right(qt,t)-1
        if i<0 or t-qt[i]>5: continue
        for side in ('yes','no'):
            bid,ask=side_q(i,side)
            if not (0.20<=ask<=0.80) or ask-bid>0.03: continue
            # momentum of the side's mid over the previous 10 s and 30 s
            def mid_at(sec):
                k=bisect.bisect_right(qt,t-sec)-1
                if k<0: return None
                b,a=side_q(k,side); return (a+b)/2
            m10=mid_at(10); 
            if m10 is None: continue
            mom=((ask+bid)/2-m10)*100
            mb='<=-3c' if mom<=-3 else '-3..-1c' if mom<-1 else '-1..+1c' if mom<=1 else '+1..+3c' if mom<3 else '>=+3c'
            for H in HOLDS:
                k=bisect.bisect_right(qt,t+H)-1
                if k<=i or t+H>close-20: continue
                eb,_=side_q(k,side)
                p=100*(eb-ask-fee(ask)-fee(max(eb,0.01)))
                res[(cls,H,mb)].append(p); allp[(cls,H)].append(p)
print('windows used',n)
print('\nBUY at the ask, SELL at the bid after a fixed hold; every 10 s of every market, ask .20-.80, spread <= 3c; cents per contract after the fee both ways')
for cls in ('crypto','commodity'):
    print(f'\n== {cls}')
    for H in HOLDS:
        L=allp[(cls,H)]
        if not L: continue
        print(f"  hold {H:3d} s: n={len(L):6d} average {S.mean(L):+.2f}c  (share that made money {100*sum(1 for x in L if x>0)/len(L):.0f}%)")
    print('  by what the price did in the 10 s BEFORE the buy (own-side mid change):')
    for mb in ('<=-3c','-3..-1c','-1..+1c','+1..+3c','>=+3c'):
        line=f"    {mb:8}"
        for H in HOLDS:
            L=res.get((cls,H,mb),[])
            line+=f" | {H:3d}s {S.mean(L):+5.2f}c (n={len(L):5d})" if len(L)>=200 else f" | {H:3d}s   n/a"
        print(line)
print('DONE')
