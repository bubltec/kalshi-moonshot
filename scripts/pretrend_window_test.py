import calendar, math, bisect, collections
from kalshi_moonshot import config, db, kalshiws
from kalshi_moonshot.store import Store
from kalshi_moonshot.strategy import fee_per_contract as fee
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,'tape',shared=True)
t0=calendar.timegm((2026,10,28,0,0,0))
rows=st.db.execute("SELECT ts,ticker,lane,side,ask,stake_usd,outcome,exit_price FROM decisions WHERE action='bet' AND mode='trade' AND stake_usd>0 AND ts>? AND lane LIKE 'crypto_%' AND lane NOT IN ('crypto_moonshot','crypto_wild_fade') AND (outcome IS NOT NULL OR exit_price IS NOT NULL) ORDER BY ts",(t0,)).fetchall()
seen={}
for r in rows: seen[(r['ticker'],r['lane'],r['side'],round(r['ask'],3))]=r
LB=(60,120,180,420)
GAP=90
out=[]
for r in seen.values():
    ser=r['ticker'].split('-')[0]; idx=kalshiws.index_for_series(ser)
    if not idx: continue
    ticks=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND ts<=? AND value>0 ORDER BY ts",(idx,r['ts']-900,r['ts'])).fetchall()
    if len(ticks)<300: continue
    T=[x['ts'] for x in ticks]; V=[x['value'] for x in ticks]
    d=[V[i+1]-V[i] for i in range(len(V)-1)]; unit=math.sqrt(sum(x*x for x in d)/len(d))*math.sqrt(60)
    if unit<=0: continue
    now=V[-1]
    def val(t):
        i=bisect.bisect_right(T,t)-1
        return V[i] if i>=0 else None
    sg=1 if r['side']=='yes' else -1
    tr={}
    for L in LB:
        a_=val(r['ts']-GAP); b=val(r['ts']-GAP-L)
        if a_ is None or b is None: break
        tr[L]=sg*(a_-b)/unit/(L/60)**0.5   # move in units of the typical move over that span
    else:
        c=r['stake_usd']/r['ask']
        p=((r['exit_price']-r['ask'])*c-(fee(r['ask'])+fee(max(r['exit_price'],.01)))*c) if r['exit_price'] is not None else (((1-r['ask'])*c if r['outcome']==r['side'] else -r['ask']*c)-fee(r['ask'])*c)
        out.append((ser,r['ts'],tr,p,r['stake_usd']))
print('bets',len(out))
def sc(v): 
    return (len(v),sum(o[3] for o in v),sum(o[4] for o in v))
def fmt(n,p,st_): return f"n={n:3d} net {p:+7.2f} on {st_:5.0f} {100*p/st_ if st_ else 0:+5.1f}%"
cut=sorted(o[1] for o in out)[len(out)//2]
print('trend measured over L seconds ENDING 90s BEFORE the buy, in sigma units; threshold +0.5 (with us) vs rest')
for L in LB:
    for nm,f in (('ALL',lambda o:True),('1st half',lambda o:o[1]<cut),('2nd half',lambda o:o[1]>=cut)):
        v=[o for o in out if f(o)]
        a=sc([o for o in v if o[2][L]>0.5]); b=sc([o for o in v if o[2][L]<=0.5])
        print(f"L={L:3d}s {nm:8} with-us {fmt(*a)} | rest {fmt(*b)}")
print('\nper-coin lookback chosen on 1st half (best with-us minus rest net), scored on 2nd half:')
tot_w=tot_r=0
for ser in sorted({o[0] for o in out}):
    c1=[o for o in out if o[0]==ser and o[1]<cut]; c2=[o for o in out if o[0]==ser and o[1]>=cut]
    if len(c1)<15 or len(c2)<15: continue
    best=max(LB,key=lambda L:sum(o[3] for o in c1 if o[2][L]>0.5)/max(1,sum(1 for o in c1 if o[2][L]>0.5))-sum(o[3] for o in c1 if o[2][L]<=0.5)/max(1,sum(1 for o in c1 if o[2][L]<=0.5)))
    w=[o for o in c2 if o[2][best]>0.5]; r_=[o for o in c2 if o[2][best]<=0.5]
    tot_w+=sum(o[3] for o in w); tot_r+=sum(o[3] for o in r_)
    print(f"  {ser:10} best L={best:3d}s  2nd half with-us {fmt(*sc(w))} | rest {fmt(*sc(r_))}")
print(f"  total 2nd half: with-us {tot_w:+.2f} | rest {tot_r:+.2f}")
print('DONE')
