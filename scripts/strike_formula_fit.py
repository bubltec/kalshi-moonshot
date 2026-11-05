import os, calendar, bisect, statistics as S
os.nice(15)
from kalshi_moonshot import config, db, variants
from kalshi_moonshot.store import Store
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
t0=calendar.timegm((2026,11,2,6,0,0))
W=st.db.execute("SELECT ticker,series,close_ts,strike FROM windows WHERE close_ts>? AND result IN ('yes','no') AND strike>0 ORDER BY close_ts",(t0,)).fetchall()
cache={}
def feed(ix):
    if ix not in cache:
        r=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(ix,t0-1500)).fetchall(); cache[ix]=([float(x[0]) for x in r],[float(x[1]) for x in r])
    return cache[ix]
def mean(ts,vs,a,b):
    i=bisect.bisect_left(ts,a); j=bisect.bisect_right(ts,b); seg=vs[i:j]
    return sum(seg)/len(seg) if len(seg)>=3 else None
def at(ts,vs,T):
    i=bisect.bisect_right(ts,T)-1
    return vs[i] if i>=0 and T-ts[i]<30 else None
cands={'spot at open':lambda t,v,o:at(t,v,o),'spot at open+30s':lambda t,v,o:at(t,v,o+30),'spot at open-30s':lambda t,v,o:at(t,v,o-30),
 'mean open-60..open':lambda t,v,o:mean(t,v,o-60,o),'mean open-30..open':lambda t,v,o:mean(t,v,o-30,o),'mean open..open+30':lambda t,v,o:mean(t,v,o,o+30),
 'mean open-30..open+30':lambda t,v,o:mean(t,v,o-30,o+30),'mean open-60..open+60':lambda t,v,o:mean(t,v,o-60,o+60),
 'mean prev 15 min':lambda t,v,o:mean(t,v,o-900,o),'mean prev 15 min + 30s':lambda t,v,o:mean(t,v,o-900,o+30)}
err={k:{} for k in cands}
for w in W:
    ser=w['series']
    if ser in variants.RECORD_ONLY: continue
    cx=variants.crypto_index(ser); ix=cx or variants.biquote_index(ser)
    if not ix: continue
    t,v=feed(ix); o=float(w['close_ts'])-900; K=float(w['strike']); cls='crypto' if cx else 'commodity'
    for k,f in cands.items():
        x=f(t,v,o)
        if x: err[k].setdefault(cls,[]).append(1e4*abs(x-K)/K)
for cls in ('crypto','commodity'):
    print(f'== {cls}: |candidate - published strike| in basis points of price')
    for k in cands:
        L=err[k].get(cls)
        if L and len(L)>30: print(f"   {k:26} n={len(L):4d} median {S.median(L):6.2f}bp  within 0.5bp {100*sum(x<=0.5 for x in L)/len(L):3.0f}%  within 2bp {100*sum(x<=2 for x in L)/len(L):3.0f}%")
print('DONE')
