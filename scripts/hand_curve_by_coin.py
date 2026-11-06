"""Your closed hand-placed crypto trips (first fill = the entry), winners (net > 0) against losers, on the curve around the buy: the market's own price for the side bought over the previous 5 minutes (qdrop = now minus the
5-minute high, qpos = where now sits in the 5-minute range, q60 = the last 60 s change) and the spot swing toward the bet (variants.wave_features amp/pos), all in that coin's own units (spot moves in its typical minutes;
the market's curve in cents). Per coin, then pooled with each feature z-scored within its coin, so what holds across markets shows up. Needs the tape (from 10/28)."""
import os, time, bisect, math, collections, statistics as S
os.nice(15)
from kalshi_moonshot import config, db, trips, variants
from kalshi_moonshot.store import Store
s=config.reload(); st=Store(s.db_path,s.exclude_versions); tape=db.connect(s.tape_path,"tape",shared=True)
T=[t for t in trips.build_trips(st) if t.source=='manual' and t.closed_ts and variants.crypto_index(t.series) and t.series.startswith('KX') and t.series.endswith('15M')]
ticks={}
def feed(ix):
    if ix not in ticks:
        r=tape.execute("SELECT ts,value FROM index_ticks WHERE index_id=? AND ts>? AND value>0 ORDER BY ts",(ix,time.time()-14*86400)).fetchall()
        ticks[ix]=([float(a) for a,_ in r],[float(b) for _,b in r])
    return ticks[ix]
rows=[]
for t in T:
    q=[(float(a),float(b),float(c)) for a,b,c in tape.execute("SELECT ts,bid,ask FROM quotes WHERE ticker=? AND bid>0 AND ask>0 ORDER BY ts",(t.ticker,)).fetchall()]
    if len(q)<60: continue
    qt=[x[0] for x in q]; at=t.opened_ts; i=bisect.bisect_right(qt,at)-1
    if i<0 or at-qt[i]>10: continue
    side=t.side
    px=lambda r: ((r[1]+r[2])/2) if side=='yes' else (1-(r[1]+r[2])/2)
    a=bisect.bisect_left(qt,at-300)
    seg=[px(r) for r in q[a:i+1]]
    if len(seg)<20: continue
    now_p=seg[-1]; hi,lo=max(seg),min(seg)
    j=bisect.bisect_right(qt,at-60)-1
    if j<0: continue
    ts,vs=feed(variants.crypto_index(t.series))
    w=variants.wave_features(ts,vs,side,at)
    if w is None: continue
    el=(at-(t.close_ts-900))/60 if t.close_ts else None
    rows.append(dict(coin=t.series.replace('KX','').replace('15M',''),win=t.pnl>0,pnl=t.pnl,price=t.entry_price,minute=el,qdrop=100*(now_p-hi),qpos=(now_p-lo)/(hi-lo) if hi>lo else 0.5,q60=100*(now_p-px(q[j])),amp=w['amp'],pos=w['pos']))
print('closed hand crypto trips with tape curve and spot history:',len(rows))
F=('price','minute','qdrop','qpos','q60','amp','pos')
def show(name,L):
    W=[x for x in L if x['win']]; Lo=[x for x in L if not x['win']]
    if len(W)<5 or len(Lo)<5: print(f'{name}: winners {len(W)} losers {len(Lo)} (too few)'); return
    print(f"{name}: {len(L)} trips, {len(W)} winners ({100*len(W)/len(L):.0f}%), net {sum(x['pnl'] for x in L):+.2f}")
    for f in F:
        a=[x[f] for x in W if x[f] is not None]; b=[x[f] for x in Lo if x[f] is not None]
        sd=S.pstdev(a+b) or 1
        print(f"    {f:7} winners {S.mean(a):+7.2f}  losers {S.mean(b):+7.2f}  gap {(S.mean(a)-S.mean(b))/sd:+5.2f} sd")
by=collections.defaultdict(list)
for x in rows: by[x['coin']].append(x)
for coin,L in sorted(by.items(),key=lambda kv:-len(kv[1])): show(coin,L)
# pooled with within-coin z-scores
Z=[]
for coin,L in by.items():
    if len(L)<10: continue
    st_={f:(S.mean(x[f] for x in L if x[f] is not None),S.pstdev([x[f] for x in L if x[f] is not None]) or 1) for f in F}
    for x in L: Z.append({**x,**{f:(x[f]-st_[f][0])/st_[f][1] for f in F if x[f] is not None}})
show('POOLED (z-scored within each coin)',Z)
print('DONE')
