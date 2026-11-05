import os, time, calendar, collections
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
t0=calendar.timegm((2026,11,2,6,0,0))
BANDS={'KXBTC15M':(0.85,0.92),'KXETH15M':(0.78,0.92),'KXDOGE15M':(0.78,0.92),'KXSOL15M':(0.78,0.92)}
W=st.db.execute("SELECT ticker,series,close_ts,result FROM windows WHERE close_ts>? AND result IN ('yes','no') AND series IN ('KXBTC15M','KXETH15M','KXDOGE15M','KXSOL15M') ORDER BY close_ts",(t0,)).fetchall()
def pnl(price,won,maker):
    c=10.0/price; return ((1-price)*c if won else -10.0)-(0 if maker else fee(price)*c)
def run(SECS,CENTS):
    out=[]
    for w in W:
        close=float(w['close_ts']); band=BANDS[w['series']]
        q=[(float(a),float(b),float(c)) for a,b,c in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND ts BETWEEN ? AND ? AND bid>0 AND ask>0 ORDER BY ts",(w['ticker'],close-480,close-60)).fetchall()]
        pk=variants.favorite_entry(q,close,band)
        if not pk: continue
        side,ask,at,bid=pk; won=w['result']==side
        def sask(row): return row[2] if side=='yes' else 1.0-row[1]
        rest=round(ask-CENTS,2)
        window=[r for r in q if at<r[0]<=at+SECS]
        filled=next((r for r in window if sask(r)<=rest+1e-9),None)
        base=pnl(ask,won,False)
        if filled:
            kind='filled'; px=rest; p=pnl(rest,won,True)
        else:
            after=[r for r in q if r[0]>=at+SECS]
            a2=sask(after[0]) if after else None
            if a2 is not None and band[0]-1e-9<=a2<=ask+0.02+1e-9:
                kind='took'; px=a2; p=pnl(a2,won,False)
            else: kind='missed'; px=None; p=0.0
        out.append((close,kind,won,p,base,px,ask))
    mid=out[len(out)//2][0]
    print(f'\n##### rest {CENTS*100:.0f}c under the ask for {SECS:.0f} s, then take at the ask if still in band and <= decision ask + 2c; {len(out)} crypto favorites since 11-02 06:00 UTC')
    for lab,sel in (('all',out),('first half',[o for o in out if o[0]<mid]),('second half',[o for o in out if o[0]>=mid])):
        n=len(sel); print(f'-- {lab}: {n} favorites; buy at once: {sum(o[4] for o in sel):+.1f} ({sum(o[4] for o in sel)/n:+.2f}/bet); rest-then-take: {sum(o[3] for o in sel):+.1f} ({sum(o[3] for o in sel)/n:+.2f}/bet)')
        for k in ('filled','took','missed'):
            g=[o for o in sel if o[1]==k]
            if g: print(f'   {k:7} n={len(g):4d} ({100*len(g)/n:.0f}%) win {100*sum(o[2] for o in g)/len(g):.0f}%  rest-plan {sum(o[3] for o in g):+7.1f}  buy-at-once on the same ones {sum(o[4] for o in g):+7.1f}')
for cfg in ((5.0,0.02),(5.0,0.01),(6.0,0.01)):
    run(*cfg)
print('DONE')
