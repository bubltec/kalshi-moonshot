import os, time, bisect, math, collections, statistics, random, calendar
os.nice(15)
from kalshi_moonshot import config, db
from kalshi_moonshot.store import Store
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
t0=calendar.timegm((2026,10,27,5,0,0))
GROUPS={'crypto':("KXBTC15M","KXETH15M","KXSOL15M","KXDOGE15M"),'treasury':("KX2YRRATE15M","KX5YRRATE15M","KX10YRRATE15M","KX30YRRATE15M"),'commodity':("KXGOLD15M","KXSILVER15M","KXWTI15M","KXBRENT15M","KXCOPPER15M","KXNATGAS15M")}
PAIRS=[(0.20,0.40),(0.30,0.60),(0.40,0.80),(0.30,0.45),(0.40,0.60),(0.50,0.80),(0.50,0.90),(0.60,0.90)]
res=collections.defaultdict(lambda:collections.defaultdict(list))
for name,series in GROUPS.items():
    W=st.db.execute(f"SELECT ticker,close_ts,result FROM windows WHERE series IN ({','.join('?'*len(series))}) AND result IN ('yes','no') AND close_ts>? ORDER BY close_ts",(*series,t0+86400)).fetchall()
    random.seed(11); W=list(W); random.shuffle(W); W=W[:900]
    for w in W:
        close=float(w['close_ts'])
        q=tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",(w['ticker'],close-905,close)).fetchall()
        if len(q)<40: continue
        for side in ('yes','no'):
            sides=[(close-t_, (float(b) if side=='yes' else 1-float(a)), (float(a) if side=='yes' else 1-float(b))) for t_,b,a in q]
            for (lo,tgt) in PAIRS:
                placed=False
                for idx,(left,sb,sa) in enumerate(sides):
                    if left>780 or left<60: continue
                    if not placed:
                        if sa>lo+0.005: placed=True       # the order rests only while the ask is above our bid (post-only would refuse a cross)
                        continue
                    if sa>lo: continue                    # filled when the ask comes down to our bid
                    hit=any(x[1]>=tgt for x in sides[idx+1:])
                    won=w['result']==side
                    pnl=(tgt-lo) if hit else ((1-lo) if won else -lo)
                    res[(lo,tgt)][name].append((pnl,hit,won)); break
print('resting buy at b (fills when the ask comes down to it), resting sell at t (fills when the bid reaches it), else hold; no fees on either leg')
print('pair      | '+' | '.join(f"{g:^34}" for g in GROUPS))
for pair in PAIRS:
    cells=[]
    for g in GROUPS:
        v=res[pair][g]
        if len(v)>=20:
            n=len(v); m=statistics.mean(x[0] for x in v); se=statistics.pstdev([x[0] for x in v])/math.sqrt(n)
            cells.append(f"n{n:4d} hit{sum(x[1] for x in v)/n*100:3.0f}% win{sum(x[2] for x in v)/n*100:3.0f}% {m*100:+5.1f}c±{se*100:.1f}")
        else: cells.append('-'.center(34))
    print(f"{pair[0]:.2f}->{pair[1]:.2f} | "+' | '.join(f"{c:34}" for c in cells))
print('DONE')
